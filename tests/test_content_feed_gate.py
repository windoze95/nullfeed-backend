"""The per-channel content gate also applies to the home feed: hidden types drop
out of new-episodes and recently-added (continue-watching is deliberately left
alone — you started those on purpose)."""

import pytest

from app.database import async_session_factory
from tests.helpers import seed_channel, seed_ref, seed_subscription, seed_video

pytestmark = pytest.mark.asyncio


async def _seed(user_id: str, hidden: list[str] | None) -> str:
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        cid = channel.id
        await seed_subscription(db, user_id, cid, hidden_content_types=hidden)
        reg = await seed_video(db, channel, title="Regular", content_type="regular")
        clip = await seed_video(db, channel, title="Clip", content_type="short")
        await seed_ref(db, user_id, reg.id)
        await seed_ref(db, user_id, clip.id)
    return cid


async def test_recently_added_drops_hidden_types(client, make_user):
    user, headers = await make_user()
    await _seed(user["id"], ["short"])
    resp = await client.get("/api/feed/recently-added", headers=headers)
    assert resp.status_code == 200
    assert {i["video"]["title"] for i in resp.json()} == {"Regular"}


async def test_new_episodes_drops_hidden_types(client, make_user):
    user, headers = await make_user()
    await _seed(user["id"], ["short"])
    resp = await client.get("/api/feed/new-episodes", headers=headers)
    assert resp.status_code == 200
    # The gate ranks over non-hidden videos, so the channel's episode is Regular
    # (never the hidden Clip).
    assert {i["video"]["title"] for i in resp.json()} == {"Regular"}


async def test_feed_unfiltered_when_nothing_hidden(client, make_user):
    user, headers = await make_user()
    await _seed(user["id"], None)
    resp = await client.get("/api/feed/recently-added", headers=headers)
    assert {i["video"]["title"] for i in resp.json()} == {"Regular", "Clip"}
