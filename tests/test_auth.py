"""Auth and profile endpoint tests (design section 1.1)."""

import hashlib
import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import app.services.youtube_import as youtube_import
from app.config import settings
from app.database import async_session_factory, engine
from app.main import app
from app.models.subscription import UserSubscription
from app.models.user import User
from app.models.user_video_ref import UserVideoRef
from app.models.video import Video
from tests.helpers import (
    IDENTITY_JSON,
    fake_completed_process,
    seed_channel,
    seed_ref,
    seed_subscription,
    seed_video,
)

pytestmark = pytest.mark.asyncio


async def test_first_user_is_admin_and_body_is_admin_ignored(client):
    resp = await client.post(
        "/api/auth/create", json={"display_name": "Alice", "is_admin": False}
    )
    assert resp.status_code == 200
    assert resp.json()["is_admin"] is True

    resp = await client.post(
        "/api/auth/create", json={"display_name": "Bob", "is_admin": True}
    )
    assert resp.status_code == 200
    assert resp.json()["is_admin"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"display_name": ""},
        {"display_name": "   "},
        {"display_name": "x" * 51},
        {},  # neither display_name nor youtube_handle
        {"display_name": "Ok", "pin": "12"},
        {"display_name": "Ok", "pin": "abcd"},
        {"display_name": "Ok", "pin": "123456789"},
    ],
)
async def test_create_validation_422(client, payload):
    resp = await client.post("/api/auth/create", json=payload)
    assert resp.status_code == 422


async def test_create_trims_display_name(client):
    resp = await client.post("/api/auth/create", json={"display_name": "  Bo  "})
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Bo"


async def test_select_pin_flow(client):
    resp = await client.post(
        "/api/auth/create", json={"display_name": "Pinned", "pin": "1234"}
    )
    assert resp.status_code == 200
    profile = resp.json()
    assert profile["has_pin"] is True

    resp = await client.post("/api/auth/select", json={"user_id": profile["id"]})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "PIN required"

    resp = await client.post(
        "/api/auth/select", json={"user_id": profile["id"], "pin": "9999"}
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Incorrect PIN"

    resp = await client.post(
        "/api/auth/select", json={"user_id": profile["id"], "pin": "1234"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["token"]
    assert data["user"]["id"] == profile["id"]
    assert data["user"]["has_pin"] is True


async def test_legacy_sha256_pin_verifies_and_upgrades(client):
    user_id = str(uuid.uuid4())
    async with async_session_factory() as db:
        db.add(
            User(
                id=user_id,
                display_name="Legacy",
                pin_hash=hashlib.sha256(b"4321").hexdigest(),
            )
        )
        await db.commit()

    resp = await client.post(
        "/api/auth/select", json={"user_id": user_id, "pin": "4321"}
    )
    assert resp.status_code == 200

    async with async_session_factory() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        assert user.pin_hash is not None
        assert user.pin_hash.startswith("scrypt$")

    # The upgraded scrypt hash still verifies.
    resp = await client.post(
        "/api/auth/select", json={"user_id": user_id, "pin": "4321"}
    )
    assert resp.status_code == 200


async def test_pin_rate_limit_429_after_five_failures(client):
    resp = await client.post(
        "/api/auth/create", json={"display_name": "Locked", "pin": "1234"}
    )
    user_id = resp.json()["id"]

    for _ in range(5):
        resp = await client.post(
            "/api/auth/select", json={"user_id": user_id, "pin": "0000"}
        )
        assert resp.status_code == 403

    # Even the correct PIN is rejected during the lockout window.
    resp = await client.post(
        "/api/auth/select", json={"user_id": user_id, "pin": "1234"}
    )
    assert resp.status_code == 429


async def test_me_and_logout(client, make_user):
    profile, headers = await make_user("Me")

    resp = await client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == profile["id"]

    resp = await client.post("/api/auth/logout", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"detail": "Logged out"}

    resp = await client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 401


async def test_session_persists_across_app_instances(client, make_user):
    _, headers = await make_user("Persistent")

    # Drop all pooled DB connections, then use a brand-new transport: the
    # session must come back from the database, not process/connection state.
    await engine.dispose()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as fresh:
        resp = await fresh.get("/api/auth/me", headers=headers)
        assert resp.status_code == 200


async def test_update_profile_self_and_admin_rules(client, make_user):
    admin, admin_headers = await make_user("Admin")
    other, other_headers = await make_user("Other")

    # Non-admins cannot modify someone else's profile.
    resp = await client.patch(
        f"/api/auth/profiles/{admin['id']}",
        json={"display_name": "Hax"},
        headers=other_headers,
    )
    assert resp.status_code == 403

    # Self-rename works.
    resp = await client.patch(
        f"/api/auth/profiles/{other['id']}",
        json={"display_name": "Renamed"},
        headers=other_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Renamed"

    # Admins can modify others: set then remove a PIN.
    resp = await client.patch(
        f"/api/auth/profiles/{other['id']}",
        json={"pin": "5678"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["has_pin"] is True

    resp = await client.patch(
        f"/api/auth/profiles/{other['id']}",
        json={"remove_pin": True},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["has_pin"] is False


async def test_delete_only_admin_409(client, make_user):
    admin, admin_headers = await make_user("Admin")
    await make_user("Other")

    resp = await client.delete(
        f"/api/auth/profiles/{admin['id']}", headers=admin_headers
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Cannot delete the only admin profile"


async def test_delete_other_requires_admin(client, make_user):
    admin, _ = await make_user("Admin")
    _, other_headers = await make_user("Other")

    resp = await client.delete(
        f"/api/auth/profiles/{admin['id']}", headers=other_headers
    )
    assert resp.status_code == 403


async def test_delete_profile_cascades_and_cleans_orphans(client, make_user):
    await make_user("Admin")  # the first user keeps an admin around
    victim, victim_headers = await make_user("Victim")

    rel_path = "chan/video.mp4"
    full_path = os.path.join(settings.media_path, rel_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "wb") as f:
        f.write(b"x" * 10)

    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="COMPLETE", file_path=rel_path)
        await seed_ref(db, victim["id"], video.id)
        await seed_subscription(db, victim["id"], channel.id)

    resp = await client.delete(
        f"/api/auth/profiles/{victim['id']}", headers=victim_headers
    )
    assert resp.status_code == 200
    assert resp.json() == {"detail": "Profile deleted"}

    # The session was cascade-deleted.
    resp = await client.get("/api/auth/me", headers=victim_headers)
    assert resp.status_code == 401

    # Refs and subscriptions are gone; the orphaned file was removed and the
    # video row reset to a re-downloadable state.
    assert not os.path.exists(full_path)
    async with async_session_factory() as db:
        refs = (
            (
                await db.execute(
                    select(UserVideoRef).where(UserVideoRef.user_id == victim["id"])
                )
            )
            .scalars()
            .all()
        )
        assert refs == []
        subs = (
            (
                await db.execute(
                    select(UserSubscription).where(
                        UserSubscription.user_id == victim["id"]
                    )
                )
            )
            .scalars()
            .all()
        )
        assert subs == []
        v = (await db.execute(select(Video).where(Video.id == video.id))).scalar_one()
        assert v.status == "CATALOGED"
        assert v.file_path is None


async def test_create_from_youtube_handle(client, monkeypatch):
    monkeypatch.setattr(
        youtube_import.subprocess,
        "run",
        lambda *a, **kw: fake_completed_process(IDENTITY_JSON),
    )

    async def fake_cache_avatar(url: str, user_id: str) -> None:
        # Simulate avatar download failure: profile must still be created.
        return None

    monkeypatch.setattr("app.api.auth.cache_avatar", fake_cache_avatar)

    resp = await client.post(
        "/api/auth/create", json={"youtube_handle": "@testchannel"}
    )
    assert resp.status_code == 200
    profile = resp.json()
    assert profile["display_name"] == "Test Channel"
    assert profile["avatar_url"] is None


async def test_create_from_youtube_handle_resolve_failure_502(client, monkeypatch):
    monkeypatch.setattr(
        youtube_import.subprocess,
        "run",
        lambda *a, **kw: fake_completed_process(
            "", returncode=1, stderr="ERROR: Unable to download webpage"
        ),
    )
    resp = await client.post("/api/auth/create", json={"youtube_handle": "@nope"})
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Could not resolve YouTube handle"
