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


async def test_access_walls_hidden_by_default(client, make_user):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        cid = channel.id
        # Subscribed but the filter is never configured (NULL) → default applies.
        await seed_subscription(db, user["id"], cid)
        await seed_video(db, channel, title="Regular", content_type="regular")
        await seed_video(db, channel, title="Members", content_type="members_only")
        await seed_video(db, channel, title="Premium", content_type="premium")

    # Members-only and premium are hidden without the viewer configuring anything.
    resp = await client.get(f"/api/channels/{cid}/videos", headers=headers)
    assert {v["title"] for v in resp.json()["items"]} == {"Regular"}

    # The channel detail surfaces the default so the menu shows them unchecked.
    resp = await client.get(f"/api/channels/{cid}", headers=headers)
    assert resp.json()["hidden_content_types"] == ["members_only", "premium"]

    # The reveal override still shows everything.
    resp = await client.get(
        f"/api/channels/{cid}/videos?include_hidden=true", headers=headers
    )
    assert {v["title"] for v in resp.json()["items"]} == {
        "Regular",
        "Members",
        "Premium",
    }


async def test_explicit_show_all_overrides_members_only_default(client, make_user):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        cid = channel.id
        await seed_subscription(db, user["id"], cid)
        await seed_video(db, channel, title="Members", content_type="members_only")

    # Explicitly showing everything (empty set) overrides the default.
    resp = await client.put(
        f"/api/channels/{cid}/content-filter",
        json={"hidden_content_types": []},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["hidden_content_types"] == []

    resp = await client.get(f"/api/channels/{cid}/videos", headers=headers)
    assert {v["title"] for v in resp.json()["items"]} == {"Members"}


async def test_channel_detail_reports_available_content_types(client, make_user):
    user, headers = await make_user()
    cid = await _seed_channel_with_videos(user["id"])  # regular, short, live
    resp = await client.get(f"/api/channels/{cid}", headers=headers)
    # Only the types this channel actually has — the filter menu offers just these.
    assert sorted(resp.json()["available_content_types"]) == [
        "live",
        "regular",
        "short",
    ]
