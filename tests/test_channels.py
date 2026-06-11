"""Channel subscribe + bulk subscribe tests (design 1.3 and 1.4)."""

from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.database import async_session_factory
from app.models.channel import Channel
from app.models.subscription import UserSubscription
from app.models.user_video_ref import UserVideoRef
from app.utils.time import utcnow_naive
from tests.helpers import seed_channel, seed_ref, seed_subscription, seed_video

pytestmark = pytest.mark.asyncio


@pytest.fixture
def poll_delay(monkeypatch):
    """Stub out Celery enqueueing so tests never touch the Redis broker."""
    mock = MagicMock()
    monkeypatch.setattr("app.api.channels.poll_channel_task.delay", mock)
    return mock


async def test_bulk_subscribe_creates_channel_without_resolution(
    client, make_user, poll_delay, monkeypatch
):
    def boom(youtube_channel_id):
        raise AssertionError(
            "fetch_channel_metadata must not be called when a name is provided"
        )

    monkeypatch.setattr("app.api.channels.fetch_channel_metadata", boom)

    _, headers = await make_user()
    resp = await client.post(
        "/api/channels/subscribe-bulk",
        json={"items": [{"youtube_channel_id": "UCnew1", "name": "Cool Channel"}]},
        headers=headers,
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[0]["status"] == "subscribed"
    channel_id = results[0]["channel_id"]

    async with async_session_factory() as db:
        channel = (
            await db.execute(select(Channel).where(Channel.id == channel_id))
        ).scalar_one()
        assert channel.name == "Cool Channel"
        assert channel.slug == "cool-channel"
        assert channel.description == ""
        assert channel.avatar_url is None
        sub = (
            await db.execute(
                select(UserSubscription).where(
                    UserSubscription.channel_id == channel_id
                )
            )
        ).scalar_one()
        assert sub.retention_policy == "KEEP_ALL"
        assert sub.tracking_mode == "FUTURE_ONLY"

    poll_delay.assert_called_once_with(channel_id)


async def test_bulk_subscribe_dedupes_slugs(client, make_user, poll_delay):
    _, headers = await make_user()
    resp = await client.post(
        "/api/channels/subscribe-bulk",
        json={
            "items": [
                {"youtube_channel_id": "UCa", "name": "Same Name"},
                {"youtube_channel_id": "UCb", "name": "Same Name"},
            ]
        },
        headers=headers,
    )
    assert resp.status_code == 200
    assert [r["status"] for r in resp.json()["results"]] == [
        "subscribed",
        "subscribed",
    ]

    async with async_session_factory() as db:
        slugs = sorted((await db.execute(select(Channel.slug))).scalars().all())
        assert slugs == ["same-name", "same-name-2"]


async def test_bulk_subscribe_existing_channel_reactivates_refs(
    client, make_user, poll_delay
):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db, youtube_channel_id="UCexisting")
        video = await seed_video(db, channel)
        await seed_ref(db, user["id"], video.id, removed_at=utcnow_naive())

    resp = await client.post(
        "/api/channels/subscribe-bulk",
        json={"items": [{"youtube_channel_id": "UCexisting"}]},
        headers=headers,
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[0]["status"] == "subscribed"
    assert results[0]["channel_id"] == channel.id

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

    # No new channel was created, so no poll was enqueued.
    poll_delay.assert_not_called()


async def test_bulk_subscribe_already_subscribed(client, make_user, poll_delay):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db, youtube_channel_id="UCdup")
        await seed_subscription(db, user["id"], channel.id)

    resp = await client.post(
        "/api/channels/subscribe-bulk",
        json={"items": [{"youtube_channel_id": "UCdup"}]},
        headers=headers,
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[0]["status"] == "already_subscribed"
    assert results[0]["channel_id"] == channel.id


async def test_bulk_subscribe_per_item_errors_isolated(
    client, make_user, poll_delay, monkeypatch
):
    def echo_back(youtube_channel_id):
        # fetch_channel_metadata echoes its input back on resolution failure.
        return {"name": youtube_channel_id, "channel_id": youtube_channel_id}

    monkeypatch.setattr("app.api.channels.fetch_channel_metadata", echo_back)

    _, headers = await make_user()
    resp = await client.post(
        "/api/channels/subscribe-bulk",
        json={
            "items": [
                {"youtube_channel_id": "UCbroken"},
                {"youtube_channel_id": "UCfine", "name": "Fine"},
            ]
        },
        headers=headers,
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[0]["status"] == "error"
    assert results[1]["status"] == "subscribed"
    assert results[1]["channel_id"] is not None


@pytest.mark.parametrize(
    "items",
    [
        [],
        [{"youtube_channel_id": f"UC{i}"} for i in range(26)],
    ],
)
async def test_bulk_subscribe_item_count_validation(client, make_user, items):
    _, headers = await make_user()
    resp = await client.post(
        "/api/channels/subscribe-bulk", json={"items": items}, headers=headers
    )
    assert resp.status_code == 422


async def test_subscribe_reactivates_soft_deleted_refs(
    client, make_user, poll_delay, monkeypatch
):
    monkeypatch.setattr(
        "app.api.channels.fetch_channel_metadata",
        lambda cid: {"channel_id": "UCsub", "name": "Sub Channel", "handle": "@sub"},
    )
    monkeypatch.setattr(
        "app.api.channels.fetch_channel_images",
        lambda cid: {"avatar_url": None, "banner_url": None},
    )

    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db, youtube_channel_id="UCsub")
        v1 = await seed_video(db, channel)
        v2 = await seed_video(db, channel)
        await seed_ref(db, user["id"], v1.id, removed_at=utcnow_naive())

    resp = await client.post(
        "/api/channels/subscribe",
        json={"youtube_channel_id": "UCsub"},
        headers=headers,
    )
    assert resp.status_code == 200

    async with async_session_factory() as db:
        refs = (
            (
                await db.execute(
                    select(UserVideoRef).where(UserVideoRef.user_id == user["id"])
                )
            )
            .scalars()
            .all()
        )
        assert {r.video_id for r in refs} == {v1.id, v2.id}
        assert all(r.removed_at is None for r in refs)


async def test_subscribe_dedupes_slug_against_existing(
    client, make_user, poll_delay, monkeypatch
):
    monkeypatch.setattr(
        "app.api.channels.fetch_channel_metadata",
        lambda cid: {"channel_id": "UCnewslug", "name": "Test Channel"},
    )
    monkeypatch.setattr(
        "app.api.channels.fetch_channel_images",
        lambda cid: {"avatar_url": None, "banner_url": None},
    )

    _, headers = await make_user()
    async with async_session_factory() as db:
        await seed_channel(db, youtube_channel_id="UCother", slug="test-channel")

    resp = await client.post(
        "/api/channels/subscribe",
        json={"youtube_channel_id": "UCnewslug"},
        headers=headers,
    )
    assert resp.status_code == 200

    async with async_session_factory() as db:
        channel = (
            await db.execute(
                select(Channel).where(Channel.youtube_channel_id == "UCnewslug")
            )
        ).scalar_one()
        assert channel.slug == "test-channel-2"
