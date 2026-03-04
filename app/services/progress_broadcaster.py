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
