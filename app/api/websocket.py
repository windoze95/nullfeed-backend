import json
import logging
from collections import defaultdict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["websocket"])
logger = logging.getLogger(__name__)

# Map of user_id -> set of active WebSocket connections
_connections: dict[str, set[WebSocket]] = defaultdict(set)


@router.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    await websocket.accept()
    _connections[user_id].add(websocket)
    logger.info("WebSocket connected: user=%s", user_id)

    try:
        while True:
            # Keep connection alive; client can send pings.
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        _connections[user_id].discard(websocket)
        if not _connections[user_id]:
            del _connections[user_id]
        logger.info("WebSocket disconnected: user=%s", user_id)


async def broadcast_to_user(user_id: str, event: dict) -> None:
    """Send an event to all WebSocket connections for a specific user."""
    sockets = _connections.get(user_id, set())
    dead: list[WebSocket] = []
    message = json.dumps(event)

    for ws in sockets:
        try:
            await ws.send_text(message)
        except Exception:
            dead.append(ws)

    for ws in dead:
        sockets.discard(ws)


async def broadcast_to_all(event: dict) -> None:
    """Send an event to all connected users."""
    for user_id in list(_connections.keys()):
        await broadcast_to_user(user_id, event)
