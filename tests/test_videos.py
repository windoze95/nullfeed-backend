"""Video progress upsert, download guard, and downloads-window tests (design 1.4)."""

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.database import async_session_factory
from app.models.user_video_ref import UserVideoRef
from app.models.video import Video
from app.utils.time import utcnow_naive
from tests.helpers import seed_channel, seed_ref, seed_video

pytestmark = pytest.mark.asyncio


@pytest.fixture
def download_delay(monkeypatch):
    """Stub out Celery enqueueing so tests never touch the Redis broker."""
    mock = MagicMock()
    monkeypatch.setattr("app.api.videos.download_video_task.delay", mock)
    return mock


async def test_progress_unknown_video_404(client, make_user):
    _, headers = await make_user()
    resp = await client.put(
        "/api/videos/does-not-exist/progress",
        json={"position_seconds": 10},
        headers=headers,
    )
    assert resp.status_code == 404


async def test_progress_negative_position_422(client, make_user):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel)

    resp = await client.put(
        f"/api/videos/{video.id}/progress",
        json={"position_seconds": -1},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_progress_upsert_double_put_single_row(client, make_user):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel)

    for position in (10, 20):
        resp = await client.put(
            f"/api/videos/{video.id}/progress",
            json={"position_seconds": position},
            headers=headers,
        )
        assert resp.status_code == 200

    async with async_session_factory() as db:
        refs = (
            (
                await db.execute(
                    select(UserVideoRef).where(UserVideoRef.video_id == video.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(refs) == 1
        assert refs[0].watch_position_seconds == 20
        assert refs[0].last_watched_at is not None


async def test_progress_reactivates_soft_deleted_ref(client, make_user):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel)
        await seed_ref(
            db,
            user["id"],
            video.id,
            watch_position_seconds=5,
            removed_at=utcnow_naive(),
        )

    resp = await client.put(
        f"/api/videos/{video.id}/progress",
        json={"position_seconds": 42},
        headers=headers,
    )
    assert resp.status_code == 200

    async with async_session_factory() as db:
        ref = (
            await db.execute(
                select(UserVideoRef).where(
                    UserVideoRef.user_id == user["id"],
                    UserVideoRef.video_id == video.id,
                )
            )
        ).scalar_one()
        assert ref.removed_at is None
        assert ref.watch_position_seconds == 42


async def test_download_quality_passthrough_and_double_enqueue_409(
    client, make_user, download_delay
):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="CATALOGED")

    resp = await client.post(
        f"/api/videos/{video.id}/download",
        json={"quality": "720p"},
        headers=headers,
    )
    assert resp.status_code == 200
    download_delay.assert_called_once_with(video.id, user["id"], quality="720p")

    async with async_session_factory() as db:
        v = (await db.execute(select(Video).where(Video.id == video.id))).scalar_one()
        assert v.status == "PENDING"

    # Double-enqueue while PENDING is rejected.
    resp = await client.post(f"/api/videos/{video.id}/download", headers=headers)
    assert resp.status_code == 409
    download_delay.assert_called_once()


async def test_download_invalid_quality_422(client, make_user, download_delay):
    _, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="CATALOGED")

    resp = await client.post(
        f"/api/videos/{video.id}/download",
        json={"quality": "144p"},
        headers=headers,
    )
    assert resp.status_code == 422
    download_delay.assert_not_called()


async def test_downloads_list_window(client, make_user):
    user, headers = await make_user()
    now = utcnow_naive()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        pending = await seed_video(db, channel, status="PENDING")
        downloading = await seed_video(db, channel, status="DOWNLOADING")
        fresh = await seed_video(db, channel, status="COMPLETE", downloaded_at=now)
        stale = await seed_video(
            db,
            channel,
            status="COMPLETE",
            downloaded_at=now - timedelta(seconds=120),
        )
        for video in (pending, downloading, fresh, stale):
            await seed_ref(db, user["id"], video.id)

    resp = await client.get("/api/videos/downloads", headers=headers)
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()}
    assert ids == {pending.id, downloading.id, fresh.id}
