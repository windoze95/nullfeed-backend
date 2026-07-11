"""Recommendation freshness: invalidate-on-unsubscribe + subscribe preserves."""

import pytest
from sqlalchemy import select

from app.database import async_session_factory
from app.models.recommendation import Recommendation
from app.services.recommendation import invalidate_recommendations
from tests.helpers import seed_channel, seed_subscription

pytestmark = pytest.mark.asyncio


@pytest.fixture
def poll_delay(monkeypatch):
    from unittest.mock import MagicMock

    mock = MagicMock()
    monkeypatch.setattr("app.api.channels.poll_channel_task.delay", mock)
    return mock


async def _seed_recs(user_id: str) -> None:
    async with async_session_factory() as db:
        db.add_all(
            [
                Recommendation(
                    user_id=user_id, channel_name="Live One", youtube_channel_id="@a"
                ),
                Recommendation(
                    user_id=user_id, channel_name="Live Two", youtube_channel_id="@b"
                ),
                Recommendation(
                    user_id=user_id,
                    channel_name="Dismissed",
                    youtube_channel_id="@c",
                    dismissed=True,
                ),
            ]
        )
        await db.commit()


async def _live_names(user_id: str) -> list[str]:
    async with async_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(Recommendation.channel_name).where(
                        Recommendation.user_id == user_id,
                        Recommendation.dismissed == False,  # noqa: E712
                    )
                )
            )
            .scalars()
            .all()
        )
    return sorted(rows)


async def _all_names(user_id: str) -> list[str]:
    async with async_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(Recommendation.channel_name).where(
                        Recommendation.user_id == user_id
                    )
                )
            )
            .scalars()
            .all()
        )
    return sorted(rows)


# --- the invalidate helper ---------------------------------------------------


async def test_invalidate_drops_live_keeps_dismissed(client, make_user):
    user, _ = await make_user()
    await _seed_recs(user["id"])

    async with async_session_factory() as db:
        await invalidate_recommendations(user["id"], db)
        await db.commit()

    assert await _live_names(user["id"]) == []
    # The dismissed row (do-not-recommend list) survives.
    assert await _all_names(user["id"]) == ["Dismissed"]


# --- API endpoints invalidate ------------------------------------------------


async def test_unsubscribe_invalidates_recommendations(client, make_user):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        await seed_subscription(db, user["id"], channel.id)
    await _seed_recs(user["id"])

    # A second user's recommendations must be untouched.
    other, _ = await make_user("Other")
    await _seed_recs(other["id"])

    resp = await client.delete(
        f"/api/channels/{channel.id}/unsubscribe", headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert await _live_names(user["id"]) == []
    assert await _all_names(user["id"]) == ["Dismissed"]
    # Isolation: the other user's live recs survive.
    assert await _live_names(other["id"]) == ["Live One", "Live Two"]


async def test_subscribe_preserves_recommendations(
    client, make_user, poll_delay, monkeypatch
):
    # Subscribing to a recommendation (the Explore "browse and pick" flow) must
    # NOT clear the whole list — the client dismisses just the picked card. Only
    # unsubscribing (a taste change) and the staleness sweep invalidate.
    monkeypatch.setattr(
        "app.api.channels.fetch_channel_metadata",
        lambda cid: {"channel_id": "UCsub", "name": "Sub Channel", "handle": "@sub"},
    )
    monkeypatch.setattr(
        "app.api.channels.fetch_channel_images",
        lambda cid: {"avatar_url": None, "banner_url": None},
    )
    user, headers = await make_user()
    await _seed_recs(user["id"])

    resp = await client.post(
        "/api/channels/subscribe",
        json={"youtube_channel_id": "UCsub"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert await _live_names(user["id"]) == ["Live One", "Live Two"]


async def test_bulk_subscribe_preserves_recommendations(client, make_user, poll_delay):
    user, headers = await make_user()
    await _seed_recs(user["id"])

    resp = await client.post(
        "/api/channels/subscribe-bulk",
        json={"items": [{"youtube_channel_id": "UCnew1", "name": "Cool Channel"}]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["status"] == "subscribed"
    assert await _live_names(user["id"]) == ["Live One", "Live Two"]
