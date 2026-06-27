"""Download reliability: watchdog, crash reaper, and per-user cancel (#31/#32)."""

import contextlib
import subprocess
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.database import async_session_factory
from app.models.user_video_ref import UserVideoRef
from app.models.video import Video
from app.services import download_manager
from app.services.download_manager import DownloadCancelled, download_video
from app.services.download_reaper import (
    STUCK_DOWNLOAD_THRESHOLD_SECONDS,
    reap_stuck_downloads,
)
from app.tasks.download_tasks import _get_sync_db
from app.utils.time import utcnow_naive
from tests.helpers import seed_channel, seed_ref, seed_video


# --- Task 1: download watchdog (silent-hang detection) ---------------------


def _patch_command_to(monkeypatch, tmp_path, argv: list[str]) -> dict:
    """Make download_video spawn ``argv`` instead of yt-dlp, returning a box
    that captures the spawned process for assertions."""
    monkeypatch.setattr(download_manager.settings, "media_path", str(tmp_path))
    monkeypatch.setattr(download_manager, "WATCHDOG_POLL_INTERVAL_SECONDS", 0.05)
    spawned: dict = {}
    real_popen = subprocess.Popen

    def fake_popen(cmd, **kwargs):
        proc = real_popen(argv, **kwargs)
        spawned["proc"] = proc
        return proc

    monkeypatch.setattr(download_manager.subprocess, "Popen", fake_popen)
    return spawned


def test_watchdog_kills_silently_hung_download(monkeypatch, tmp_path):
    # A download that emits no output and never exits must be killed and the
    # call must fail — the bug was that the post-loop wait() never ran on a hang.
    monkeypatch.setattr(download_manager, "NO_OUTPUT_TIMEOUT_SECONDS", 0.3)
    spawned = _patch_command_to(monkeypatch, tmp_path, ["sleep", "60"])

    with pytest.raises(RuntimeError, match="stalled"):
        download_video("vid123", "test-slug")

    assert spawned["proc"].poll() is not None  # process was killed, not leaked


def test_watchdog_honors_cancel_during_silent_hang(monkeypatch, tmp_path):
    # Cancellation must work even when the process produces no output (the old
    # per-line cancel check could never fire during a silent hang).
    monkeypatch.setattr(download_manager, "NO_OUTPUT_TIMEOUT_SECONDS", 100.0)
    spawned = _patch_command_to(monkeypatch, tmp_path, ["sleep", "60"])

    with pytest.raises(DownloadCancelled):
        download_video("vid123", "test-slug", cancel_check=lambda: True)

    assert spawned["proc"].poll() is not None


# --- Task 1: terminal failure path -----------------------------------------


@pytest.fixture
def eager_celery(monkeypatch):
    from app.tasks.celery_app import celery_app

    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(celery_app.conf, "task_eager_propagates", True)
    return celery_app


@pytest.mark.asyncio
async def test_download_task_marks_failed_when_download_stalls(
    client, make_user, monkeypatch, eager_celery
):
    await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="PENDING")
    vid = video.id

    import app.tasks.download_tasks as dt

    def stalled(**kwargs):
        raise RuntimeError("Download stalled (no output for 300s): x")

    monkeypatch.setattr(dt, "download_video", stalled)
    # No retries left -> the stall is terminal and the row must end FAILED.
    monkeypatch.setattr(dt.download_video_task, "max_retries", 0)

    with contextlib.suppress(Exception):
        dt.download_video_task.delay(vid)

    async with async_session_factory() as db:
        status = (
            await db.execute(select(Video.status).where(Video.id == vid))
        ).scalar_one()
    assert status == "FAILED"


# --- Task 1: crash reaper ---------------------------------------------------


@pytest.mark.asyncio
async def test_reaper_resets_stranded_rows_and_leaves_fresh(client, make_user):
    await make_user()
    now = utcnow_naive()
    stale = now - timedelta(seconds=STUCK_DOWNLOAD_THRESHOLD_SECONDS + 60)

    async with async_session_factory() as db:
        channel = await seed_channel(db)
        stale_dl = await seed_video(db, channel, status="DOWNLOADING")
        stale_cancel = await seed_video(db, channel, status="CANCELLING")
        fresh_dl = await seed_video(db, channel, status="DOWNLOADING")
        null_dl = await seed_video(db, channel, status="DOWNLOADING")
        stale_dl.download_heartbeat_at = stale
        stale_cancel.download_heartbeat_at = stale
        fresh_dl.download_heartbeat_at = now
        null_dl.download_heartbeat_at = None  # predates the column / never beat
        await db.commit()
        ids = {
            "stale_dl": stale_dl.id,
            "stale_cancel": stale_cancel.id,
            "fresh_dl": fresh_dl.id,
            "null_dl": null_dl.id,
        }

    # The reaper runs in Celery with a sync session, like the real task.
    sync_db = _get_sync_db()
    try:
        result = reap_stuck_downloads(sync_db)
    finally:
        sync_db.close()

    assert set(result["requeue_ids"]) == {ids["stale_dl"], ids["null_dl"]}
    assert result["reset_cancelling"] == 1

    async with async_session_factory() as db:
        rows = (await db.execute(select(Video.id, Video.status))).all()
    status_by_id = dict(rows)
    assert status_by_id[ids["stale_dl"]] == "PENDING"
    assert status_by_id[ids["null_dl"]] == "PENDING"
    assert status_by_id[ids["stale_cancel"]] == "CATALOGED"
    assert status_by_id[ids["fresh_dl"]] == "DOWNLOADING"  # live download untouched


# --- Task 2: per-user cancel ref-counting -----------------------------------


@pytest.mark.asyncio
async def test_cancel_by_one_subscriber_does_not_stop_shared_download(
    client, make_user
):
    user_a, headers_a = await make_user("A")
    user_b, headers_b = await make_user("B")
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="DOWNLOADING")
        await seed_ref(db, user_a["id"], video.id)
        await seed_ref(db, user_b["id"], video.id)
    vid = video.id

    # A cancels: B still wants it, so the download keeps running.
    resp = await client.post(f"/api/videos/{vid}/cancel", headers=headers_a)
    assert resp.status_code == 200
    assert resp.json()["stopped"] is False

    async with async_session_factory() as db:
        status = (
            await db.execute(select(Video.status).where(Video.id == vid))
        ).scalar_one()
        a_ref = (
            await db.execute(
                select(UserVideoRef).where(
                    UserVideoRef.user_id == user_a["id"],
                    UserVideoRef.video_id == vid,
                )
            )
        ).scalar_one()
        b_ref = (
            await db.execute(
                select(UserVideoRef).where(
                    UserVideoRef.user_id == user_b["id"],
                    UserVideoRef.video_id == vid,
                )
            )
        ).scalar_one()
    assert status == "DOWNLOADING"  # B's download is untouched
    assert a_ref.removed_at is not None  # A's intent was dropped
    assert b_ref.removed_at is None  # B's intent survives

    # B cancels too: nobody is left, so the shared download is torn down.
    resp = await client.post(f"/api/videos/{vid}/cancel", headers=headers_b)
    assert resp.status_code == 200
    assert resp.json()["stopped"] is True

    async with async_session_factory() as db:
        status = (
            await db.execute(select(Video.status).where(Video.id == vid))
        ).scalar_one()
    assert status == "CANCELLING"


@pytest.mark.asyncio
async def test_trigger_download_registers_active_ref(client, make_user, monkeypatch):
    monkeypatch.setattr("app.api.videos.download_video_task.delay", MagicMock())
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="CATALOGED")
    vid = video.id

    resp = await client.post(f"/api/videos/{vid}/download", headers=headers)
    assert resp.status_code == 200

    async with async_session_factory() as db:
        ref = (
            await db.execute(
                select(UserVideoRef).where(
                    UserVideoRef.user_id == user["id"],
                    UserVideoRef.video_id == vid,
                    UserVideoRef.removed_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    assert ref is not None
