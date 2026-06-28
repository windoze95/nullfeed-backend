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


async def test_new_episodes_newest_per_channel_and_bounded_by_limit(client, make_user):
    """With a large library, the result is the newest unwatched video per
    channel, capped at ``limit`` and ordered by upload recency — regardless of
    how many videos each channel holds (the SQL window-function dedup)."""
    user, headers = await make_user()
    now = utcnow_naive()
    channel_count = 6
    videos_per_channel = 5

    expected_newest: dict[str, str] = {}  # channel_id -> newest unwatched video id
    channels = []
    async with async_session_factory() as db:
        for c in range(channel_count):
            channel = await seed_channel(db, name=f"Channel {c}")
            await seed_subscription(db, user["id"], channel.id)
            channels.append(channel)
            # Channel c's newest upload is at (now - c hours), so channel 0 has
            # the most recent newest video, channel 1 the next, and so on.
            newest = None
            for v in range(videos_per_channel):
                uploaded = now - timedelta(hours=c, days=v)
                video = await seed_video(
                    db, channel, status="COMPLETE", uploaded_at=uploaded
                )
                await seed_ref(db, user["id"], video.id)
                if v == 0:
                    newest = video
            assert newest is not None
            expected_newest[channel.id] = newest.id

    limit = 4
    resp = await client.get(f"/api/feed/new-episodes?limit={limit}", headers=headers)
    assert resp.status_code == 200
    items = resp.json()

    # Bounded by limit even though the library has channel_count * vids rows.
    assert len(items) == limit
    # No channel appears twice (dedup happened in SQL).
    channel_ids = [item["channel"]["id"] for item in items]
    assert len(set(channel_ids)) == len(channel_ids)
    # Each returned video is its channel's newest unwatched upload.
    for item in items:
        assert item["video"]["id"] == expected_newest[item["video"]["channel_id"]]
    # Ordered by upload recency: the first ``limit`` channels (0..limit-1).
    assert channel_ids == [channels[i].id for i in range(limit)]


async def test_new_episodes_ranks_only_unwatched_complete_in_channel(client, make_user):
    """The window-function ranking runs over the filtered set, so a channel's
    newest *eligible* video wins even when more-recent uploads are watched,
    removed, or still downloading."""
    user, headers = await make_user()
    now = utcnow_naive()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        await seed_subscription(db, user["id"], channel.id)

        # Newer than the answer, but each disqualified for a different reason.
        watched = await seed_video(db, channel, status="COMPLETE", uploaded_at=now)
        await seed_ref(db, user["id"], watched.id, is_watched=True)
        removed = await seed_video(
            db, channel, status="COMPLETE", uploaded_at=now - timedelta(hours=1)
        )
        await seed_ref(db, user["id"], removed.id, removed_at=now)
        downloading = await seed_video(
            db, channel, status="DOWNLOADING", uploaded_at=now - timedelta(hours=2)
        )
        await seed_ref(db, user["id"], downloading.id)

        # The newest video that is unwatched, present, and COMPLETE.
        answer = await seed_video(
            db, channel, status="COMPLETE", uploaded_at=now - timedelta(hours=3)
        )
        await seed_ref(db, user["id"], answer.id)
        # An older COMPLETE/unwatched video must lose to ``answer``.
        await seed_ref(
            db,
            user["id"],
            (
                await seed_video(
                    db,
                    channel,
                    status="COMPLETE",
                    uploaded_at=now - timedelta(hours=4),
                )
            ).id,
        )

        # A video in a channel the user is not subscribed to is ignored.
        other_channel = await seed_channel(db, name="Unsubscribed")
        unsub = await seed_video(db, other_channel, status="COMPLETE", uploaded_at=now)
        await seed_ref(db, user["id"], unsub.id)

    resp = await client.get("/api/feed/new-episodes", headers=headers)
    assert resp.status_code == 200
    items = resp.json()
    assert [item["video"]["id"] for item in items] == [answer.id]
