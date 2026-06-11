import json
import logging
from collections import defaultdict

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.api.auth import validate_token

router = APIRouter(tags=["websocket"])
logger = logging.getLogger(__name__)

# Map of user_id -> set of active WebSocket connections
_connections: dict[str, set[WebSocket]] = defaultdict(set)


@router.websocket("/ws/{user_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    user_id: str,
    token: str | None = Query(None),
) -> None:
    await websocket.accept()

    # The token must resolve to a session belonging to this user.
    token_user_id = await validate_token(token) if token else None
    if token_user_id != user_id:
        logger.info("WebSocket rejected (bad token): user=%s", user_id)
        await websocket.close(code=4401)
        return

    _connections[user_id].add(websocket)
    logger.info("WebSocket connected: user=%s", user_id)

    try:
        while True:
            # Keep connection alive; client can send pings.
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        _connections[user_id].discard(websocket)
        if not _connections[user_id]:
            del _connections[user_id]
        logger.info("WebSocket disconnected: user=%s", user_id)


async def broadcast_to_user(user_id: str, event: dict) -> None:
    """Send an event to all WebSocket connections for a specific user."""
    sockets = _connections.get(user_id)
    if not sockets:
        return
    message = json.dumps(event)

    # Iterate over a copy: sends can yield to the event loop, during which
    # connects/disconnects may mutate the underlying set.
    for ws in list(sockets):
        try:
            await ws.send_text(message)
        except Exception:
            sockets.discard(ws)


async def broadcast_to_all(event: dict) -> None:
    """Send an event to all connected users."""
    for user_id in list(_connections.keys()):
        await broadcast_to_user(user_id, event)
