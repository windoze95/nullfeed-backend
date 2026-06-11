"""Regression tests for the adversarial-review fixes on this branch."""

from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.api.channels import _extract_channel_id
from app.database import async_session_factory
from app.models.user_video_ref import UserVideoRef
from app.services.media_server import _parse_range
from tests.helpers import seed_channel, seed_ref, seed_video

pytestmark = pytest.mark.asyncio


@pytest.fixture
def download_delay(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr("app.api.videos.download_video_task.delay", mock)
    return mock


# --- cancel state machine -------------------------------------------------


async def test_cancel_inflight_download_moves_to_cancelling(client, make_user):
    _, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="DOWNLOADING")

    resp = await client.post(f"/api/videos/{video.id}/cancel", headers=headers)
    assert resp.status_code == 200

    async with async_session_factory() as db:
        from app.models.video import Video

        status = (
            await db.execute(select(Video.status).where(Video.id == video.id))
        ).scalar_one()
    assert status == "CANCELLING"


async def test_cancel_pending_download_goes_straight_to_cataloged(client, make_user):
    _, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="PENDING")

    await client.post(f"/api/videos/{video.id}/cancel", headers=headers)

    async with async_session_factory() as db:
        from app.models.video import Video

        status = (
            await db.execute(select(Video.status).where(Video.id == video.id))
        ).scalar_one()
    assert status == "CATALOGED"


async def test_redownload_blocked_while_cancelling_and_second_cancel_clears(
    client, make_user, download_delay
):
    _, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="CANCELLING")

    resp = await client.post(f"/api/videos/{video.id}/download", headers=headers)
    assert resp.status_code == 409
    download_delay.assert_not_called()

    # Second cancel is the force-clear escape hatch.
    resp = await client.post(f"/api/videos/{video.id}/cancel", headers=headers)
    assert resp.status_code == 200
    resp = await client.post(f"/api/videos/{video.id}/download", headers=headers)
    assert resp.status_code == 200
    download_delay.assert_called_once()


# --- is_watched derivation -------------------------------------------------


async def test_progress_near_end_marks_watched(client, make_user):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="COMPLETE")
        video.duration_seconds = 600
        await db.commit()

    resp = await client.put(
        f"/api/videos/{video.id}/progress",
        json={"position_seconds": 590},
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
        assert ref.is_watched is True

    # Restarting the video makes it in-progress again.
    await client.put(
        f"/api/videos/{video.id}/progress",
        json={"position_seconds": 30},
        headers=headers,
    )
    async with async_session_factory() as db:
        ref = (
            await db.execute(
                select(UserVideoRef).where(
                    UserVideoRef.user_id == user["id"],
                    UserVideoRef.video_id == video.id,
                )
            )
        ).scalar_one()
        assert ref.is_watched is False


async def test_progress_mid_video_not_watched(client, make_user):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="COMPLETE")
        video.duration_seconds = 600
        await db.commit()

    await client.put(
        f"/api/videos/{video.id}/progress",
        json={"position_seconds": 300},
        headers=headers,
    )
    async with async_session_factory() as db:
        ref = (
            await db.execute(
                select(UserVideoRef).where(
                    UserVideoRef.user_id == user["id"],
                    UserVideoRef.video_id == video.id,
                )
            )
        ).scalar_one()
        assert ref.is_watched is False


# --- channel videos expose last_watched_at ----------------------------------


async def test_channel_videos_include_last_watched_at(client, make_user):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="COMPLETE")
        await seed_ref(db, user["id"], video.id)

    await client.put(
        f"/api/videos/{video.id}/progress",
        json={"position_seconds": 42},
        headers=headers,
    )
    resp = await client.get(f"/api/channels/{channel.id}/videos", headers=headers)
    items = resp.json()["items"]
    assert items[0]["last_watched_at"] is not None


# --- range parsing hardening -------------------------------------------------


def test_parse_range_rejects_non_ascii_digits():
    # latin-1 superscript two passes str.isdigit() but crashes int().
    assert _parse_range("bytes=\xb2-", 1000) is None
    assert _parse_range("bytes=0-\xb3", 1000) is None
    assert _parse_range("bytes=-\xb9", 1000) is None


def test_parse_range_still_accepts_normal_ranges():
    assert _parse_range("bytes=0-", 1000) == (0, 999)
    assert _parse_range("bytes=10-19", 1000) == (10, 19)
    assert _parse_range("bytes=-10", 1000) == (990, 999)


# --- subscribe accepts bare handles ------------------------------------------


def test_extract_channel_id_accepts_bare_handle_and_uc_id():
    assert _extract_channel_id("@mkbhd") == "@mkbhd"
    assert _extract_channel_id("UCBJycsmduvYEL83R_U4JriQ") == (
        "UCBJycsmduvYEL83R_U4JriQ"
    )
    assert _extract_channel_id("https://www.youtube.com/@mkbhd") == "@mkbhd"
    assert (
        _extract_channel_id("https://www.youtube.com/channel/UCBJycsmduvYEL83R_U4JriQ")
        == "UCBJycsmduvYEL83R_U4JriQ"
    )
    assert _extract_channel_id("not a channel") is None


# --- cataloged videos sort by catalog time when upload date is unknown -------


async def test_channel_videos_nulls_uploaded_at_sort_first(client, make_user):
    from datetime import datetime, timedelta

    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        old = await seed_video(
            db, channel, title="Old", uploaded_at=datetime(2026, 3, 3)
        )
        fresh = await seed_video(db, channel, title="Fresh Catalog")
        fresh.uploaded_at = None
        fresh.created_at = old.created_at + timedelta(days=90)
        await db.commit()

    resp = await client.get(f"/api/channels/{channel.id}/videos", headers=headers)
    titles = [v["title"] for v in resp.json()["items"]]
    assert titles == ["Fresh Catalog", "Old"]
