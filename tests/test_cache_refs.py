"""Library-vs-cache ref kind: playing caches, downloading promotes, listings
filter (downloads-as-cache, #86)."""

from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.database import async_session_factory
from app.models.user_video_ref import (
    REF_KIND_CACHE,
    REF_KIND_LIBRARY,
    UserVideoRef,
)
from app.models.video import Video
from tests.helpers import seed_channel, seed_ref, seed_video

pytestmark = pytest.mark.asyncio


@pytest.fixture
def download_delay(monkeypatch):
    """Stub Celery enqueue so tests never touch the Redis broker."""
    mock = MagicMock()
    monkeypatch.setattr("app.api.videos.download_video_task.delay", mock)
    return mock


async def _get_ref(user_id: str, video_id: str) -> UserVideoRef:
    async with async_session_factory() as db:
        return (
            await db.execute(
                select(UserVideoRef).where(
                    UserVideoRef.user_id == user_id,
                    UserVideoRef.video_id == video_id,
                )
            )
        ).scalar_one()


async def test_progress_creates_cache_ref(client, make_user):
    """Watching a not-downloaded video records a CACHE ref, not a library one."""
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="CATALOGED")

    resp = await client.put(
        f"/api/videos/{video.id}/progress",
        json={"position_seconds": 12},
        headers=headers,
    )
    assert resp.status_code == 200
    ref = await _get_ref(user["id"], video.id)
    assert ref.kind == REF_KIND_CACHE
    assert ref.removed_at is None


async def test_progress_does_not_downgrade_library_ref(client, make_user):
    """Watching a library video leaves it LIBRARY (conflict path keeps kind)."""
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="COMPLETE")
        await seed_ref(db, user["id"], video.id, kind=REF_KIND_LIBRARY)

    resp = await client.put(
        f"/api/videos/{video.id}/progress",
        json={"position_seconds": 5},
        headers=headers,
    )
    assert resp.status_code == 200
    ref = await _get_ref(user["id"], video.id)
    assert ref.kind == REF_KIND_LIBRARY


async def test_cache_endpoint_creates_cache_ref_and_enqueues(
    client, make_user, download_delay
):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="CATALOGED")

    resp = await client.post(f"/api/videos/{video.id}/cache", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "PENDING"
    download_delay.assert_called_once_with(video.id, user["id"])

    ref = await _get_ref(user["id"], video.id)
    assert ref.kind == REF_KIND_CACHE

    async with async_session_factory() as db:
        v = (await db.execute(select(Video).where(Video.id == video.id))).scalar_one()
        assert v.status == "PENDING"


async def test_cache_endpoint_complete_video_does_not_enqueue(
    client, make_user, download_delay
):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="COMPLETE")

    resp = await client.post(f"/api/videos/{video.id}/cache", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "COMPLETE"
    download_delay.assert_not_called()
    # It still records the cache claim so the ref-count keeps the file alive.
    ref = await _get_ref(user["id"], video.id)
    assert ref.kind == REF_KIND_CACHE


async def test_download_promotes_cache_ref_to_library(
    client, make_user, download_delay
):
    """A cold-watched (CACHE) video that the user then downloads joins library."""
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="CATALOGED")
        await seed_ref(db, user["id"], video.id, kind=REF_KIND_CACHE)

    resp = await client.post(f"/api/videos/{video.id}/download", headers=headers)
    assert resp.status_code == 200
    download_delay.assert_called_once()
    ref = await _get_ref(user["id"], video.id)
    assert ref.kind == REF_KIND_LIBRARY


async def test_library_grid_excludes_cache_refs(client, make_user):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        lib = await seed_video(db, channel, status="COMPLETE", title="Lib")
        cached = await seed_video(db, channel, status="COMPLETE", title="Cached")
        await seed_ref(db, user["id"], lib.id, kind=REF_KIND_LIBRARY)
        await seed_ref(db, user["id"], cached.id, kind=REF_KIND_CACHE)

    resp = await client.get("/api/videos", headers=headers)
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["items"]}
    assert lib.id in ids
    assert cached.id not in ids


async def test_downloads_list_excludes_cache(client, make_user):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        lib = await seed_video(db, channel, status="DOWNLOADING")
        cached = await seed_video(db, channel, status="DOWNLOADING")
        await seed_ref(db, user["id"], lib.id, kind=REF_KIND_LIBRARY)
        await seed_ref(db, user["id"], cached.id, kind=REF_KIND_CACHE)

    resp = await client.get("/api/videos/downloads", headers=headers)
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()}
    assert lib.id in ids
    assert cached.id not in ids


async def test_continue_watching_includes_cache(client, make_user):
    """A mid-watch cache video still shows in Continue Watching."""
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        cached = await seed_video(db, channel, status="COMPLETE")
        await seed_ref(
            db,
            user["id"],
            cached.id,
            kind=REF_KIND_CACHE,
            watch_position_seconds=30,
            is_watched=False,
        )

    resp = await client.get("/api/feed/continue-watching", headers=headers)
    assert resp.status_code == 200
    ids = {item["video"]["id"] for item in resp.json()}
    assert cached.id in ids
