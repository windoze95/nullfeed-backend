"""Per-channel content-type gate: set/clear which types are hidden for a channel,
and filter the channel's video list accordingly (with a reveal override)."""

import pytest

from app.database import async_session_factory
from tests.helpers import seed_channel, seed_subscription, seed_video

pytestmark = pytest.mark.asyncio


async def _seed_channel_with_videos(user_id: str) -> str:
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        cid = channel.id
        await seed_subscription(db, user_id, cid)
        await seed_video(db, channel, title="Regular", content_type="regular")
        await seed_video(db, channel, title="Clip", content_type="short")
        await seed_video(db, channel, title="Stream", content_type="live")
    return cid


async def test_set_and_get_content_filter(client, make_user):
    user, headers = await make_user()
    cid = await _seed_channel_with_videos(user["id"])

    resp = await client.put(
        f"/api/channels/{cid}/content-filter",
        json={"hidden_content_types": ["short", "live"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert sorted(resp.json()["hidden_content_types"]) == ["live", "short"]

    # Persisted: the channel detail reflects it.
    resp = await client.get(f"/api/channels/{cid}", headers=headers)
    assert sorted(resp.json()["hidden_content_types"]) == ["live", "short"]


async def test_content_filter_rejects_unknown_type(client, make_user):
    user, headers = await make_user()
    cid = await _seed_channel_with_videos(user["id"])
    resp = await client.put(
        f"/api/channels/{cid}/content-filter",
        json={"hidden_content_types": ["short", "bogus"]},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_content_filter_requires_subscription(client, make_user):
    await make_user()  # a different, unsubscribed viewer
    _, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        cid = channel.id
    resp = await client.put(
        f"/api/channels/{cid}/content-filter",
        json={"hidden_content_types": ["short"]},
        headers=headers,
    )
    assert resp.status_code == 404


async def test_channel_videos_hides_and_reveals(client, make_user):
    user, headers = await make_user()
    cid = await _seed_channel_with_videos(user["id"])

    await client.put(
        f"/api/channels/{cid}/content-filter",
        json={"hidden_content_types": ["short", "live"]},
        headers=headers,
    )

    resp = await client.get(f"/api/channels/{cid}/videos", headers=headers)
    body = resp.json()
    assert {v["title"] for v in body["items"]} == {"Regular"}
    assert body["total"] == 1

    # The reveal override brings the hidden ones back.
    resp = await client.get(
        f"/api/channels/{cid}/videos?include_hidden=true", headers=headers
    )
    body = resp.json()
    assert {v["title"] for v in body["items"]} == {"Regular", "Clip", "Stream"}
    assert body["total"] == 3


async def test_null_content_type_treated_as_regular(client, make_user):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        cid = channel.id
        await seed_subscription(db, user["id"], cid)
        # A pre-existing row with no content_type (cataloged before the field).
        await seed_video(db, channel, title="Legacy", content_type=None)

    # Hiding shorts leaves the untyped (regular) row visible…
    await client.put(
        f"/api/channels/{cid}/content-filter",
        json={"hidden_content_types": ["short"]},
        headers=headers,
    )
    resp = await client.get(f"/api/channels/{cid}/videos", headers=headers)
    assert {v["title"] for v in resp.json()["items"]} == {"Legacy"}

    # …but hiding regular hides it too (NULL counts as regular).
    await client.put(
        f"/api/channels/{cid}/content-filter",
        json={"hidden_content_types": ["regular"]},
        headers=headers,
    )
    resp = await client.get(f"/api/channels/{cid}/videos", headers=headers)
    assert resp.json()["items"] == []


async def test_empty_filter_clears(client, make_user):
    user, headers = await make_user()
    cid = await _seed_channel_with_videos(user["id"])
    await client.put(
        f"/api/channels/{cid}/content-filter",
        json={"hidden_content_types": ["short"]},
        headers=headers,
    )
    resp = await client.put(
        f"/api/channels/{cid}/content-filter",
        json={"hidden_content_types": []},
        headers=headers,
    )
    assert resp.json()["hidden_content_types"] == []
    resp = await client.get(f"/api/channels/{cid}/videos", headers=headers)
    assert len(resp.json()["items"]) == 3
