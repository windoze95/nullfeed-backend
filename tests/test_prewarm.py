"""Predictive preview pre-warming: POST /api/videos/prewarm (#87)."""

from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.api.videos import PREWARM_MAX_PER_CALL
from app.database import async_session_factory
from app.models.video import Video
from tests.helpers import seed_channel, seed_video

pytestmark = pytest.mark.asyncio


@pytest.fixture
def preview_delay(monkeypatch):
    """Stub the Celery preview enqueue so tests never touch the broker."""
    mock = MagicMock()
    monkeypatch.setattr("app.api.videos.download_preview_task.delay", mock)
    return mock


async def _preview_status(video_id: str) -> str | None:
    async with async_session_factory() as db:
        return (
            await db.execute(select(Video.preview_status).where(Video.id == video_id))
        ).scalar_one()


async def test_prewarm_requires_auth(client):
    resp = await client.post("/api/videos/prewarm", json={"video_ids": ["x"]})
    assert resp.status_code == 401


async def test_prewarm_enqueues_eligible(client, make_user, preview_delay):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        cataloged = await seed_video(db, channel, status="CATALOGED")
        # HQ downloading but no preview yet — a preview still helps (play now).
        downloading = await seed_video(db, channel, status="DOWNLOADING")

    resp = await client.post(
        "/api/videos/prewarm",
        json={"video_ids": [cataloged.id, downloading.id]},
        headers=headers,
    )
    assert resp.status_code == 200
    assert set(resp.json()["enqueued"]) == {cataloged.id, downloading.id}
    assert preview_delay.call_count == 2
    # Marked DOWNLOADING up front so a repeat call skips it.
    assert await _preview_status(cataloged.id) == "DOWNLOADING"


async def test_prewarm_skips_complete_and_already_previewed(
    client, make_user, preview_delay
):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        complete = await seed_video(db, channel, status="COMPLETE")
        ready = await seed_video(
            db, channel, status="CATALOGED", preview_status="READY"
        )
        in_progress = await seed_video(
            db, channel, status="CATALOGED", preview_status="DOWNLOADING"
        )
        eligible = await seed_video(db, channel, status="CATALOGED")

    resp = await client.post(
        "/api/videos/prewarm",
        json={
            "video_ids": [complete.id, ready.id, in_progress.id, eligible.id],
        },
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["enqueued"] == [eligible.id]
    preview_delay.assert_called_once_with(eligible.id, user["id"])


async def test_prewarm_dedupes_and_caps(client, make_user, preview_delay):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        videos = [
            await seed_video(db, channel, status="CATALOGED")
            for _ in range(PREWARM_MAX_PER_CALL + 5)
        ]

    ids = [v.id for v in videos]
    # Send every id twice; the endpoint dedupes, then caps.
    resp = await client.post(
        "/api/videos/prewarm", json={"video_ids": ids + ids}, headers=headers
    )
    assert resp.status_code == 200
    assert len(resp.json()["enqueued"]) == PREWARM_MAX_PER_CALL
    assert preview_delay.call_count == PREWARM_MAX_PER_CALL


async def test_prewarm_empty_list_is_noop(client, make_user, preview_delay):
    user, headers = await make_user()
    resp = await client.post(
        "/api/videos/prewarm", json={"video_ids": []}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["enqueued"] == []
    preview_delay.assert_not_called()
