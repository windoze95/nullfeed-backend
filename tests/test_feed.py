"""Feed ordering tests (design 1.4)."""

from datetime import timedelta

import pytest

from app.database import async_session_factory
from app.utils.time import utcnow_naive
from tests.helpers import seed_channel, seed_ref, seed_subscription, seed_video

pytestmark = pytest.mark.asyncio


async def test_continue_watching_ordered_by_last_watched(client, make_user):
    user, headers = await make_user()
    now = utcnow_naive()
    async with async_session_factory() as db:
        channel_a = await seed_channel(db, name="Channel A")
        channel_b = await seed_channel(db, name="Channel B")
        video_a = await seed_video(db, channel_a, status="COMPLETE")
        video_b = await seed_video(db, channel_b, status="COMPLETE")
        await seed_ref(
            db,
            user["id"],
            video_a.id,
            watch_position_seconds=30,
            last_watched_at=now - timedelta(hours=2),
        )
        await seed_ref(
            db,
            user["id"],
            video_b.id,
            watch_position_seconds=10,
            last_watched_at=now - timedelta(minutes=5),
        )

    resp = await client.get("/api/feed/continue-watching", headers=headers)
    assert resp.status_code == 200
    items = resp.json()
    assert [item["video"]["id"] for item in items] == [video_b.id, video_a.id]


async def test_recently_added_ordered_by_downloaded_at(client, make_user):
    user, headers = await make_user()
    now = utcnow_naive()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        old = await seed_video(
            db,
            channel,
            status="COMPLETE",
            downloaded_at=now - timedelta(days=1),
        )
        new = await seed_video(db, channel, status="COMPLETE", downloaded_at=now)
        # Legacy row without downloaded_at sorts last (NULLS LAST).
        legacy = await seed_video(db, channel, status="COMPLETE")
        for video in (old, new, legacy):
            await seed_ref(db, user["id"], video.id)

    resp = await client.get("/api/feed/recently-added", headers=headers)
    assert resp.status_code == 200
    items = resp.json()
    assert [item["video"]["id"] for item in items] == [new.id, old.id, legacy.id]


async def test_home_feed_aggregates_all_sections(client, make_user):
    user, headers = await make_user()
    now = utcnow_naive()
    async with async_session_factory() as db:
        ch1 = await seed_channel(db, name="Channel 1")
        ch2 = await seed_channel(db, name="Channel 2")
        await seed_subscription(db, user["id"], ch1.id)
        await seed_subscription(db, user["id"], ch2.id)

        # continue-watching: partial progress on ch1
        watching = await seed_video(db, ch1, status="COMPLETE")
        await seed_ref(
            db, user["id"], watching.id, watch_position_seconds=30, last_watched_at=now
        )
        # a fresh unwatched episode on ch2
        fresh = await seed_video(db, ch2, status="COMPLETE", downloaded_at=now)
        await seed_ref(db, user["id"], fresh.id)

    resp = await client.get("/api/feed/home", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == {"continue_watching", "new_episodes", "recently_added"}

    # Same per-section logic as the individual endpoints.
    assert [i["video"]["id"] for i in data["continue_watching"]] == [watching.id]
    assert fresh.id in {i["video"]["id"] for i in data["new_episodes"]}
    assert {watching.id, fresh.id} <= {i["video"]["id"] for i in data["recently_added"]}


async def test_home_feed_requires_auth(client):
    assert (await client.get("/api/feed/home")).status_code == 401
