"""Short-lived signed stream/WS tickets and ticket-or-token acceptance (#30).

Covers the ticket helper crypto, the mint endpoints, and that ``/stream``,
``/preview-stream`` and the WebSocket accept a valid ticket while still honoring
the legacy ``?token=`` session param during the transition.
"""

import os
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import WebSocketDisconnect

import app.api.websocket as websocket_api
from app.config import settings
from app.database import async_session_factory
from app.utils.tickets import (
    SCOPE_STREAM,
    SCOPE_WS,
    TicketError,
    mint_ticket,
    verify_ticket,
)
from tests.helpers import seed_channel, seed_video

CONTENT = b"0123456789" * 10  # 100 bytes


# --- ticket helper (pure crypto) -------------------------------------------


def test_mint_verify_roundtrip_stream():
    ticket, expires_in = mint_ticket(SCOPE_STREAM, "user-1", video_id="vid-1")
    assert expires_in > 0
    assert verify_ticket(ticket, scope=SCOPE_STREAM, video_id="vid-1") == "user-1"


def test_mint_verify_roundtrip_ws():
    ticket, _ = mint_ticket(SCOPE_WS, "user-1")
    assert verify_ticket(ticket, scope=SCOPE_WS, user_id="user-1") == "user-1"


def test_verify_rejects_wrong_scope():
    ticket, _ = mint_ticket(SCOPE_WS, "user-1")
    with pytest.raises(TicketError):
        verify_ticket(ticket, scope=SCOPE_STREAM, video_id="vid-1")


def test_verify_rejects_expired():
    ticket, _ = mint_ticket(SCOPE_STREAM, "user-1", video_id="vid-1", ttl_seconds=-1)
    with pytest.raises(TicketError):
        verify_ticket(ticket, scope=SCOPE_STREAM, video_id="vid-1")


def test_verify_rejects_wrong_video():
    ticket, _ = mint_ticket(SCOPE_STREAM, "user-1", video_id="vid-1")
    with pytest.raises(TicketError):
        verify_ticket(ticket, scope=SCOPE_STREAM, video_id="vid-2")


def test_verify_rejects_wrong_user():
    ticket, _ = mint_ticket(SCOPE_WS, "user-1")
    with pytest.raises(TicketError):
        verify_ticket(ticket, scope=SCOPE_WS, user_id="user-2")


def test_verify_rejects_tampered_signature():
    ticket, _ = mint_ticket(SCOPE_STREAM, "user-1", video_id="vid-1")
    tampered = ticket[:-1] + ("A" if ticket[-1] != "A" else "B")
    with pytest.raises(TicketError):
        verify_ticket(tampered, scope=SCOPE_STREAM, video_id="vid-1")


def test_verify_rejects_tampered_payload():
    segment, signature = mint_ticket(SCOPE_STREAM, "u", video_id="vid-1")[0].split(".")
    forged_segment = mint_ticket(SCOPE_STREAM, "u", video_id="vid-EVIL")[0].split(".")[
        0
    ]
    with pytest.raises(TicketError):
        verify_ticket(f"{forged_segment}.{signature}", scope=SCOPE_STREAM)


@pytest.mark.parametrize("bad", ["", "no-dot", "a.b.c", "....", "only."])
def test_verify_rejects_malformed(bad):
    with pytest.raises(TicketError):
        verify_ticket(bad, scope=SCOPE_STREAM)


def test_secret_change_invalidates_tickets(monkeypatch):
    monkeypatch.setattr(settings, "stream_ticket_secret", "secret-A")
    ticket, _ = mint_ticket(SCOPE_WS, "user-1")
    assert verify_ticket(ticket, scope=SCOPE_WS) == "user-1"
    monkeypatch.setattr(settings, "stream_ticket_secret", "secret-B")
    with pytest.raises(TicketError):
        verify_ticket(ticket, scope=SCOPE_WS)


# --- mint endpoints ---------------------------------------------------------


@pytest.mark.asyncio
async def test_playback_ticket_requires_session(client):
    resp = await client.post(f"/api/videos/{uuid.uuid4()}/playback-ticket")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_playback_ticket_unknown_video_404(client, make_user):
    _, headers = await make_user()
    resp = await client.post(
        f"/api/videos/{uuid.uuid4()}/playback-ticket", headers=headers
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_ws_ticket_requires_session(client):
    resp = await client.post("/api/auth/ws-ticket")
    assert resp.status_code == 401


# --- /stream and /preview-stream accept ticket OR token ---------------------


async def _seed_complete_video(rel_path: str, *, preview: bool = False):
    full_path = os.path.join(settings.media_path, rel_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "wb") as f:
        f.write(CONTENT)
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        if preview:
            video = await seed_video(
                db,
                channel,
                preview_status="READY",
                preview_file_path=rel_path,
            )
        else:
            video = await seed_video(db, channel, status="COMPLETE", file_path=rel_path)
    return video


@pytest.mark.asyncio
async def test_mint_then_stream_with_ticket(client, make_user):
    _, headers = await make_user()
    video = await _seed_complete_video(f"chan/{uuid.uuid4().hex}.mp4")

    mint = await client.post(f"/api/videos/{video.id}/playback-ticket", headers=headers)
    assert mint.status_code == 200, mint.text
    body = mint.json()
    assert body["expires_in"] > 0
    ticket = body["ticket"]

    # The ticket alone authorizes the stream; no session token in the URL.
    resp = await client.get(f"/api/videos/{video.id}/stream?ticket={ticket}")
    assert resp.status_code == 200
    assert resp.content == CONTENT

    # Range requests still work over a ticket.
    resp = await client.get(
        f"/api/videos/{video.id}/stream?ticket={ticket}",
        headers={"Range": "bytes=10-19"},
    )
    assert resp.status_code == 206
    assert resp.content == CONTENT[10:20]


@pytest.mark.asyncio
async def test_stream_token_still_works(client, make_user):
    _, headers = await make_user()
    token = headers["X-User-Token"]
    video = await _seed_complete_video(f"chan/{uuid.uuid4().hex}.mp4")

    resp = await client.get(f"/api/videos/{video.id}/stream?token={token}")
    assert resp.status_code == 200
    assert resp.content == CONTENT


@pytest.mark.asyncio
async def test_stream_expired_ticket_rejected(client, make_user):
    user, _ = await make_user()
    video = await _seed_complete_video(f"chan/{uuid.uuid4().hex}.mp4")
    ticket, _ = mint_ticket(SCOPE_STREAM, user["id"], video_id=video.id, ttl_seconds=-1)

    resp = await client.get(f"/api/videos/{video.id}/stream?ticket={ticket}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_stream_ticket_for_other_video_rejected(client, make_user):
    user, _ = await make_user()
    video = await _seed_complete_video(f"chan/{uuid.uuid4().hex}.mp4")
    # A valid ticket, but minted for a different video id.
    ticket, _ = mint_ticket(SCOPE_STREAM, user["id"], video_id="some-other-video")

    resp = await client.get(f"/api/videos/{video.id}/stream?ticket={ticket}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_stream_tampered_ticket_rejected(client, make_user):
    user, _ = await make_user()
    video = await _seed_complete_video(f"chan/{uuid.uuid4().hex}.mp4")
    ticket, _ = mint_ticket(SCOPE_STREAM, user["id"], video_id=video.id)
    tampered = ticket[:-1] + ("A" if ticket[-1] != "A" else "B")

    resp = await client.get(f"/api/videos/{video.id}/stream?ticket={tampered}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_stream_ticket_falls_back_to_token_when_invalid(client, make_user):
    """A present-but-invalid ticket must not block a valid session token."""
    _, headers = await make_user()
    token = headers["X-User-Token"]
    video = await _seed_complete_video(f"chan/{uuid.uuid4().hex}.mp4")

    resp = await client.get(
        f"/api/videos/{video.id}/stream?ticket=garbage&token={token}"
    )
    assert resp.status_code == 200
    assert resp.content == CONTENT


@pytest.mark.asyncio
async def test_preview_stream_with_ticket(client, make_user):
    _, headers = await make_user()
    video = await _seed_complete_video(
        f"chan/{uuid.uuid4().hex}.preview.mp4", preview=True
    )
    ticket, _ = mint_ticket(SCOPE_STREAM, "ignored", video_id=video.id)

    resp = await client.get(f"/api/videos/{video.id}/preview-stream?ticket={ticket}")
    assert resp.status_code == 200
    assert resp.content == CONTENT


# --- WebSocket accepts ticket OR token --------------------------------------


@pytest.mark.asyncio
async def test_ws_accepts_valid_ticket():
    # Pass token=None explicitly: a direct call would otherwise see the Query()
    # default object (the ASGI layer resolves an absent param to None for us).
    ticket, _ = mint_ticket(SCOPE_WS, "u1")
    ws = AsyncMock()
    ws.receive_text = AsyncMock(side_effect=WebSocketDisconnect())
    await websocket_api.websocket_endpoint(ws, user_id="u1", ticket=ticket, token=None)
    ws.accept.assert_awaited_once()
    ws.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_ws_ticket_for_other_user_closes_4401():
    ticket, _ = mint_ticket(SCOPE_WS, "u2")
    ws = AsyncMock()
    await websocket_api.websocket_endpoint(ws, user_id="u1", ticket=ticket, token=None)
    ws.close.assert_awaited_once_with(code=4401)


@pytest.mark.asyncio
async def test_ws_bad_ticket_closes_4401():
    ws = AsyncMock()
    await websocket_api.websocket_endpoint(
        ws, user_id="u1", ticket="garbage", token=None
    )
    ws.close.assert_awaited_once_with(code=4401)


@pytest.mark.asyncio
async def test_ws_token_still_works(monkeypatch):
    async def fake_validate(_token):
        return "u1"

    monkeypatch.setattr(websocket_api, "validate_token", fake_validate)
    ws = AsyncMock()
    ws.receive_text = AsyncMock(side_effect=WebSocketDisconnect())
    await websocket_api.websocket_endpoint(
        ws, user_id="u1", ticket=None, token="good-token"
    )
    ws.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_ws_invalid_ticket_falls_back_to_token(monkeypatch):
    async def fake_validate(_token):
        return "u1"

    monkeypatch.setattr(websocket_api, "validate_token", fake_validate)
    ws = AsyncMock()
    ws.receive_text = AsyncMock(side_effect=WebSocketDisconnect())
    await websocket_api.websocket_endpoint(
        ws, user_id="u1", ticket="garbage", token="good-token"
    )
    ws.close.assert_not_awaited()
