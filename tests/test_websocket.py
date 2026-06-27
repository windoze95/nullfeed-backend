"""WebSocket auth tests: a bad or absent token must close with code 4401 (#25)."""

from unittest.mock import AsyncMock

import pytest

import app.api.websocket as websocket_api
from app.main import app


@pytest.mark.asyncio
async def test_ws_absent_token_closes_4401():
    ws = AsyncMock()
    await websocket_api.websocket_endpoint(ws, user_id="u1", token=None)
    ws.accept.assert_awaited_once()
    ws.close.assert_awaited_once_with(code=4401)


@pytest.mark.asyncio
async def test_ws_unresolvable_token_closes_4401(monkeypatch):
    async def fake_validate(_token):
        return None  # token resolves to no session

    monkeypatch.setattr(websocket_api, "validate_token", fake_validate)
    ws = AsyncMock()
    await websocket_api.websocket_endpoint(ws, user_id="u1", token="garbage")
    ws.close.assert_awaited_once_with(code=4401)


@pytest.mark.asyncio
async def test_ws_token_for_other_user_closes_4401(monkeypatch):
    async def fake_validate(_token):
        return "someone-else"  # valid token, but for a different user

    monkeypatch.setattr(websocket_api, "validate_token", fake_validate)
    ws = AsyncMock()
    await websocket_api.websocket_endpoint(ws, user_id="u1", token="valid-but-other")
    ws.close.assert_awaited_once_with(code=4401)


def test_ws_absent_token_integration_closes_4401():
    """End-to-end through the ASGI stack: the handshake is accepted then closed.

    TestClient is intentionally not entered as a context manager so the app
    lifespan (which would start the Redis progress listener) does not run; the
    absent-token path never touches the database either.
    """
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/u1") as ws:
            ws.receive_text()
    assert exc_info.value.code == 4401
