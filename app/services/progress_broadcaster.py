import asyncio
import json
import logging

import redis
import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)

PROGRESS_CHANNEL = "download_progress"


_sync_redis: redis.Redis | None = None


def _get_sync_redis() -> redis.Redis:
    global _sync_redis
    if _sync_redis is None:
        _sync_redis = redis.from_url(settings.redis_url)
    return _sync_redis


def publish_download_progress(video_id: str, user_id: str, percentage: float) -> None:
    """Publish download progress from the Celery worker (sync)."""
    _get_sync_redis().publish(
        PROGRESS_CHANNEL,
        json.dumps({
            "video_id": video_id,
            "user_id": user_id,
            "percentage": round(percentage, 1),
        }),
    )


def publish_preview_ready(video_id: str, user_id: str) -> None:
    """Publish preview_ready event from the Celery worker (sync)."""
    _get_sync_redis().publish(
        PROGRESS_CHANNEL,
        json.dumps({
            "type": "preview_ready",
            "video_id": video_id,
            "user_id": user_id,
        }),
    )


def publish_download_complete(video_id: str, user_id: str, channel_id: str | None = None) -> None:
    """Publish download_complete event from the Celery worker (sync)."""
    _get_sync_redis().publish(
        PROGRESS_CHANNEL,
        json.dumps({
            "type": "download_complete",
            "video_id": video_id,
            "user_id": user_id,
            "channel_id": channel_id,
        }),
    )


async def start_progress_listener() -> None:
    """Subscribe to the progress channel and forward events via WebSocket."""
    from app.api.websocket import broadcast_to_user

    r = aioredis.from_url(settings.redis_url)
    pubsub = r.pubsub()
    await pubsub.subscribe(PROGRESS_CHANNEL)

    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                payload = json.loads(message["data"])
                msg_type = payload.get("type")

                if msg_type == "preview_ready":
                    await broadcast_to_user(
                        payload["user_id"],
                        {
                            "type": "preview_ready",
                            "data": {"video_id": payload["video_id"]},
                        },
                    )
                elif msg_type == "download_complete":
                    data = {"video_id": payload["video_id"]}
                    if payload.get("channel_id"):
                        data["channel_id"] = payload["channel_id"]
                    await broadcast_to_user(
                        payload["user_id"],
                        {
                            "type": "download_complete",
                            "data": data,
                        },
                    )
                else:
                    # Default: download_progress (backward compatible)
                    await broadcast_to_user(
                        payload["user_id"],
                        {
                            "type": "download_progress",
                            "data": {
                                "video_id": payload["video_id"],
                                "percentage": payload["percentage"],
                            },
                        },
                    )
            except Exception:
                logger.exception("Error processing progress message")
    except asyncio.CancelledError:
        pass
    finally:
        await pubsub.unsubscribe(PROGRESS_CHANNEL)
        await r.aclose()
