"""Search + cursor-pagination tests for channels and videos."""

from datetime import datetime, timedelta

import pytest

from app.database import async_session_factory
from app.utils.pagination import decode_cursor, encode_cursor
from app.utils.search import escape_like
from tests.helpers import seed_channel, seed_ref, seed_video

pytestmark = pytest.mark.asyncio


# --- channel name search -----------------------------------------------------


async def test_channels_filter_by_name_case_insensitive(client, make_user):
    _, headers = await make_user()
    async with async_session_factory() as db:
        await seed_channel(db, name="Linus Tech Tips", youtube_channel_id="UCltt")
        await seed_channel(db, name="Marques Brownlee", youtube_channel_id="UCmkbhd")

    resp = await client.get("/api/channels", params={"q": "linus"}, headers=headers)
    assert resp.status_code == 200
    names = [c["name"] for c in resp.json()]
    assert names == ["Linus Tech Tips"]

    # Case-insensitive, substring anywhere in the name.
    resp = await client.get("/api/channels", params={"q": "BROWN"}, headers=headers)
    assert [c["name"] for c in resp.json()] == ["Marques Brownlee"]


async def test_channels_no_query_returns_all(client, make_user):
    _, headers = await make_user()
    async with async_session_factory() as db:
        await seed_channel(db, name="Alpha", youtube_channel_id="UCa")
        await seed_channel(db, name="Beta", youtube_channel_id="UCb")

    resp = await client.get("/api/channels", headers=headers)
    assert resp.status_code == 200
    assert sorted(c["name"] for c in resp.json()) == ["Alpha", "Beta"]


async def test_channels_query_escapes_like_wildcards(client, make_user):
    _, headers = await make_user()
    async with async_session_factory() as db:
        await seed_channel(db, name="100% Cotton", youtube_channel_id="UC1")
        await seed_channel(db, name="Plain Channel", youtube_channel_id="UC2")

    # A literal "%" must not behave as a wildcard matching everything.
    resp = await client.get("/api/channels", params={"q": "%"}, headers=headers)
    assert [c["name"] for c in resp.json()] == ["100% Cotton"]


# --- video search ------------------------------------------------------------


async def test_video_search_by_title(client, make_user):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        match = await seed_video(db, channel, title="Rust Programming Tutorial")
        other = await seed_video(db, channel, title="Cooking with Gas")
        await seed_ref(db, user["id"], match.id)
        await seed_ref(db, user["id"], other.id)

    resp = await client.get("/api/videos", params={"q": "rust"}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert [v["title"] for v in body["items"]] == ["Rust Programming Tutorial"]
    assert body["next_cursor"] is None


async def test_video_search_matches_channel_name(client, make_user):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db, name="The Rust Foundation")
        video = await seed_video(db, channel, title="Unrelated Title")
        await seed_ref(db, user["id"], video.id)

    resp = await client.get("/api/videos", params={"q": "rust"}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert [v["id"] for v in body["items"]] == [video.id]
    assert body["items"][0]["channel_name"] == "The Rust Foundation"


async def test_video_search_scoped_to_user(client, make_user):
    """Only the caller's active refs are searchable."""
    user_a, headers_a = await make_user("A")
    user_b, headers_b = await make_user("B")
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        owned = await seed_video(db, channel, title="Shared Topic A")
        foreign = await seed_video(db, channel, title="Shared Topic B")
        removed = await seed_video(db, channel, title="Shared Topic C")
        await seed_ref(db, user_a["id"], owned.id)
        await seed_ref(db, user_b["id"], foreign.id)
        # A soft-deleted ref must not surface in A's results.
        await seed_ref(db, user_a["id"], removed.id, removed_at=datetime(2026, 1, 1))

    resp = await client.get("/api/videos", params={"q": "shared"}, headers=headers_a)
    assert [v["title"] for v in resp.json()["items"]] == ["Shared Topic A"]


async def test_video_search_empty_query_lists_library(client, make_user):
    """Absent/blank q returns the whole library, not an error or empty list."""
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        v1 = await seed_video(db, channel, title="One")
        v2 = await seed_video(db, channel, title="Two")
        await seed_ref(db, user["id"], v1.id)
        await seed_ref(db, user["id"], v2.id)

    for params in ({}, {"q": ""}, {"q": "   "}):
        resp = await client.get("/api/videos", params=params, headers=headers)
        assert resp.status_code == 200, params
        assert resp.json()["total"] == 2, params


async def test_video_search_filters_status_watched_channel(client, make_user):
    user, headers = await make_user()
    async with async_session_factory() as db:
        ch1 = await seed_channel(db, youtube_channel_id="UCone")
        ch2 = await seed_channel(db, youtube_channel_id="UCtwo")
        complete = await seed_video(db, ch1, title="Done", status="COMPLETE")
        cataloged = await seed_video(db, ch1, title="Todo", status="CATALOGED")
        other_ch = await seed_video(db, ch2, title="Elsewhere", status="COMPLETE")
        await seed_ref(db, user["id"], complete.id, is_watched=True)
        await seed_ref(db, user["id"], cataloged.id, is_watched=False)
        await seed_ref(db, user["id"], other_ch.id, is_watched=False)

    # status filter
    resp = await client.get(
        "/api/videos", params={"status": "CATALOGED"}, headers=headers
    )
    assert [v["title"] for v in resp.json()["items"]] == ["Todo"]

    # watched filter (per-user)
    resp = await client.get("/api/videos", params={"watched": "true"}, headers=headers)
    assert [v["title"] for v in resp.json()["items"]] == ["Done"]

    resp = await client.get("/api/videos", params={"watched": "false"}, headers=headers)
    assert sorted(v["title"] for v in resp.json()["items"]) == ["Elsewhere", "Todo"]

    # channel_id filter
    resp = await client.get(
        "/api/videos", params={"channel_id": ch2.id}, headers=headers
    )
    assert [v["title"] for v in resp.json()["items"]] == ["Elsewhere"]


async def test_video_search_pagination_cursor(client, make_user):
    user, headers = await make_user()
    base = datetime(2026, 1, 1, 12, 0, 0)
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        # Five videos, strictly increasing upload time => deterministic order.
        for i in range(5):
            v = await seed_video(
                db, channel, title=f"Ep {i}", uploaded_at=base + timedelta(hours=i)
            )
            await seed_ref(db, user["id"], v.id)

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        resp = await client.get("/api/videos", params=params, headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 5  # total is stable across pages
        seen.extend(v["title"] for v in body["items"])
        cursor = body["next_cursor"]
        pages += 1
        if cursor is None:
            break
        assert pages < 10  # guard against an infinite loop

    # Newest-first, every item exactly once.
    assert seen == ["Ep 4", "Ep 3", "Ep 2", "Ep 1", "Ep 0"]
    assert len(seen) == len(set(seen))


async def test_video_search_invalid_cursor_returns_400(client, make_user):
    _, headers = await make_user()
    resp = await client.get(
        "/api/videos", params={"cursor": "not-a-valid-cursor"}, headers=headers
    )
    assert resp.status_code == 400


async def test_video_search_requires_auth(client):
    resp = await client.get("/api/videos")
    assert resp.status_code == 401


# --- channel videos stay on offset pagination (unchanged contract) -----------


async def test_channel_videos_offset_pagination_unchanged(client, make_user):
    """GET /api/channels/{id}/videos keeps its original page/per_page contract."""
    user, headers = await make_user()
    base = datetime(2026, 2, 1, 8, 0, 0)
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        for i in range(3):
            await seed_video(
                db, channel, title=f"V{i}", uploaded_at=base + timedelta(hours=i)
            )

    resp = await client.get(
        f"/api/channels/{channel.id}/videos",
        params={"page": 1, "per_page": 2},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert body["page"] == 1
    assert body["per_page"] == 2
    assert "next_cursor" not in body  # the cursor envelope is /api/videos-only
    assert [v["title"] for v in body["items"]] == ["V2", "V1"]

    resp = await client.get(
        f"/api/channels/{channel.id}/videos",
        params={"page": 2, "per_page": 2},
        headers=headers,
    )
    body2 = resp.json()
    assert body2["page"] == 2
    assert [v["title"] for v in body2["items"]] == ["V0"]


# --- cursor + escape helpers (unit) ------------------------------------------


def test_cursor_roundtrip():
    dt = datetime(2026, 6, 27, 13, 14, 15, 123456)
    token = encode_cursor(dt, "abc-123")
    assert decode_cursor(token) == (dt, "abc-123")


def test_decode_cursor_rejects_garbage():
    assert decode_cursor("@@@not-base64@@@") is None
    assert decode_cursor("") is None


def test_escape_like_neutralizes_wildcards():
    assert escape_like("100%_x") == "100\\%\\_x"
    assert escape_like("a\\b") == "a\\\\b"
