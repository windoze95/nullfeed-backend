"""Watch-later queue: add/remove idempotency, ordering, isolation, pagination."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.database import async_session_factory
from app.models.user_queue import UserQueue
from tests.helpers import seed_channel, seed_queue, seed_ref, seed_video

pytestmark = pytest.mark.asyncio


# --- add ---------------------------------------------------------------------


async def test_add_and_list_queue(client, make_user):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, title="Watch This Later")

    resp = await client.post(f"/api/videos/{video.id}/queue", headers=headers)
    assert resp.status_code == 200, resp.text

    resp = await client.get("/api/queue", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["next_cursor"] is None
    assert [v["id"] for v in body["items"]] == [video.id]
    assert body["items"][0]["title"] == "Watch This Later"
    assert body["items"][0]["channel_name"] == channel.name


async def test_add_to_queue_idempotent(client, make_user):
    """Re-adding the same video keeps a single row."""
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel)

    for _ in range(3):
        resp = await client.post(f"/api/videos/{video.id}/queue", headers=headers)
        assert resp.status_code == 200

    async with async_session_factory() as db:
        rows = (
            (await db.execute(select(UserQueue).where(UserQueue.user_id == user["id"])))
            .scalars()
            .all()
        )
    assert len(rows) == 1

    resp = await client.get("/api/queue", headers=headers)
    assert resp.json()["total"] == 1


async def test_add_to_queue_unknown_video_404(client, make_user):
    _, headers = await make_user()
    resp = await client.post("/api/videos/does-not-exist/queue", headers=headers)
    assert resp.status_code == 404


async def test_readd_preserves_position(client, make_user):
    """An idempotent re-add must NOT move the video to the end of the queue."""
    user, headers = await make_user()
    base = datetime(2026, 1, 1, 12, 0, 0)
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        a = await seed_video(db, channel, title="A")
        b = await seed_video(db, channel, title="B")
        c = await seed_video(db, channel, title="C")
        await seed_queue(db, user["id"], a.id, added_at=base)
        await seed_queue(db, user["id"], b.id, added_at=base + timedelta(minutes=1))
        await seed_queue(db, user["id"], c.id, added_at=base + timedelta(minutes=2))

    # Re-add A (the head). Its original, earliest added_at must be preserved, so
    # the order stays A, B, C rather than B, C, A.
    resp = await client.post(f"/api/videos/{a.id}/queue", headers=headers)
    assert resp.status_code == 200

    resp = await client.get("/api/queue", headers=headers)
    assert [v["title"] for v in resp.json()["items"]] == ["A", "B", "C"]


# --- ordering ----------------------------------------------------------------


async def test_queue_orders_by_added_at_not_insertion(client, make_user):
    """Order is by added_at (oldest first), independent of row insert order."""
    user, headers = await make_user()
    base = datetime(2026, 3, 1, 9, 0, 0)
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        a = await seed_video(db, channel, title="A")
        b = await seed_video(db, channel, title="B")
        c = await seed_video(db, channel, title="C")
        # Insert out of order; added_at decides the result order.
        await seed_queue(db, user["id"], c.id, added_at=base + timedelta(minutes=2))
        await seed_queue(db, user["id"], a.id, added_at=base)
        await seed_queue(db, user["id"], b.id, added_at=base + timedelta(minutes=1))

    resp = await client.get("/api/queue", headers=headers)
    assert [v["title"] for v in resp.json()["items"]] == ["A", "B", "C"]


# --- remove ------------------------------------------------------------------


async def test_remove_from_queue_idempotent(client, make_user):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel)
        await seed_queue(db, user["id"], video.id)

    # First removal empties the queue.
    resp = await client.delete(f"/api/videos/{video.id}/queue", headers=headers)
    assert resp.status_code == 200
    resp = await client.get("/api/queue", headers=headers)
    assert resp.json()["total"] == 0

    # Removing again is a no-op success (idempotent).
    resp = await client.delete(f"/api/videos/{video.id}/queue", headers=headers)
    assert resp.status_code == 200


async def test_remove_unknown_video_is_idempotent_not_404(client, make_user):
    """Removing a video that was never queued (or doesn't exist) still 200s."""
    _, headers = await make_user()
    resp = await client.delete("/api/videos/does-not-exist/queue", headers=headers)
    assert resp.status_code == 200


# --- per-user isolation ------------------------------------------------------


async def test_queue_is_per_user(client, make_user):
    user_a, headers_a = await make_user("A")
    user_b, headers_b = await make_user("B")
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        shared = await seed_video(db, channel, title="Shared")
        only_b = await seed_video(db, channel, title="OnlyB")
        # Both queue the shared video; only B queues the second.
        await seed_queue(db, user_a["id"], shared.id)
        await seed_queue(db, user_b["id"], shared.id)
        await seed_queue(db, user_b["id"], only_b.id)

    resp = await client.get("/api/queue", headers=headers_a)
    assert {v["id"] for v in resp.json()["items"]} == {shared.id}

    resp = await client.get("/api/queue", headers=headers_b)
    assert {v["id"] for v in resp.json()["items"]} == {shared.id, only_b.id}

    # A removing the shared video must not touch B's identical entry.
    resp = await client.delete(f"/api/videos/{shared.id}/queue", headers=headers_a)
    assert resp.status_code == 200

    resp = await client.get("/api/queue", headers=headers_a)
    assert resp.json()["total"] == 0
    resp = await client.get("/api/queue", headers=headers_b)
    assert {v["id"] for v in resp.json()["items"]} == {shared.id, only_b.id}


async def test_deleting_profile_clears_queue(client, make_user):
    """Profile deletion must remove the user's queue rows.

    With SQLite foreign keys enforced, a leftover queue row would make the
    users-row delete fail, so this also guards the FK-safe cleanup order.
    """
    await make_user("Admin")  # keep an admin around so the victim can be deleted
    victim, victim_headers = await make_user("Victim")
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel)
        await seed_queue(db, victim["id"], video.id)

    resp = await client.delete(
        f"/api/auth/profiles/{victim['id']}", headers=victim_headers
    )
    assert resp.status_code == 200

    async with async_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(UserQueue).where(UserQueue.user_id == victim["id"])
                )
            )
            .scalars()
            .all()
        )
    assert rows == []


# --- progress population (queue is independent of download refs) --------------


async def test_queue_item_progress_and_works_without_ref(client, make_user):
    """A queued video surfaces watch progress when a ref exists, and still
    appears (with default progress) when the user holds no ref."""
    user, headers = await make_user()
    base = datetime(2026, 4, 1, 0, 0, 0)
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        watched = await seed_video(db, channel, title="Watched")
        never = await seed_video(db, channel, title="Never Opened")
        await seed_queue(db, user["id"], watched.id, added_at=base)
        await seed_queue(db, user["id"], never.id, added_at=base + timedelta(minutes=1))
        # A ref only for the first video.
        await seed_ref(
            db, user["id"], watched.id, watch_position_seconds=42, is_watched=True
        )

    resp = await client.get("/api/queue", headers=headers)
    items = {v["title"]: v for v in resp.json()["items"]}
    assert set(items) == {"Watched", "Never Opened"}
    assert items["Watched"]["watch_position_seconds"] == 42
    assert items["Watched"]["is_watched"] is True
    assert items["Never Opened"]["watch_position_seconds"] == 0
    assert items["Never Opened"]["is_watched"] is False


async def test_queue_ignores_soft_deleted_ref(client, make_user):
    """A soft-deleted ref must not leak progress; the item still appears."""
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, title="Soft Deleted Ref")
        await seed_queue(db, user["id"], video.id)
        await seed_ref(
            db,
            user["id"],
            video.id,
            watch_position_seconds=99,
            removed_at=datetime(2026, 1, 1),
        )

    resp = await client.get("/api/queue", headers=headers)
    items = resp.json()["items"]
    assert [v["id"] for v in items] == [video.id]
    assert items[0]["watch_position_seconds"] == 0


# --- pagination --------------------------------------------------------------


async def test_queue_pagination_cursor(client, make_user):
    user, headers = await make_user()
    base = datetime(2026, 5, 1, 12, 0, 0)
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        for i in range(5):
            v = await seed_video(db, channel, title=f"Ep {i}")
            await seed_queue(db, user["id"], v.id, added_at=base + timedelta(minutes=i))

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        resp = await client.get("/api/queue", params=params, headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 5  # total is stable across pages
        seen.extend(v["title"] for v in body["items"])
        cursor = body["next_cursor"]
        pages += 1
        if cursor is None:
            break
        assert pages < 10  # guard against an infinite loop

    # Oldest-first (FIFO), every item exactly once.
    assert seen == ["Ep 0", "Ep 1", "Ep 2", "Ep 3", "Ep 4"]
    assert len(seen) == len(set(seen))


async def test_queue_invalid_cursor_400(client, make_user):
    _, headers = await make_user()
    resp = await client.get(
        "/api/queue", params={"cursor": "not-a-valid-cursor"}, headers=headers
    )
    assert resp.status_code == 400


async def test_queue_empty(client, make_user):
    _, headers = await make_user()
    resp = await client.get("/api/queue", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"items": [], "total": 0, "next_cursor": None}


# --- auth --------------------------------------------------------------------


async def test_queue_endpoints_require_auth(client):
    assert (await client.get("/api/queue")).status_code == 401
    assert (await client.post("/api/videos/x/queue")).status_code == 401
    assert (await client.delete("/api/videos/x/queue")).status_code == 401
