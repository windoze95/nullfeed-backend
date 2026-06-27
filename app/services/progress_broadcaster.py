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
        json.dumps(
            {
                "video_id": video_id,
                "user_id": user_id,
                "percentage": round(percentage, 1),
            }
        ),
    )


def publish_preview_ready(video_id: str, user_id: str) -> None:
    """Publish preview_ready event from the Celery worker (sync)."""
    _get_sync_redis().publish(
        PROGRESS_CHANNEL,
        json.dumps(
            {
                "type": "preview_ready",
                "video_id": video_id,
                "user_id": user_id,
            }
        ),
    )


def publish_download_complete(
    video_id: str,
    user_id: str,
    channel_id: str | None = None,
    title: str | None = None,
    youtube_video_id: str | None = None,
) -> None:
    """Publish download_complete event from the Celery worker (sync)."""
    _get_sync_redis().publish(
        PROGRESS_CHANNEL,
        json.dumps(
            {
                "type": "download_complete",
                "video_id": video_id,
                "user_id": user_id,
                "channel_id": channel_id,
                "title": title,
                "youtube_video_id": youtube_video_id,
            }
        ),
    )


def publish_new_episode(
    video_id: str,
    user_id: str,
    channel_id: str | None = None,
    title: str | None = None,
    youtube_video_id: str | None = None,
) -> None:
    """Publish new_episode event from the Celery worker (sync).

    Emitted when the poller catalogs a genuinely new video for a subscriber.
    """
    _get_sync_redis().publish(
        PROGRESS_CHANNEL,
        json.dumps(
            {
                "type": "new_episode",
                "video_id": video_id,
                "user_id": user_id,
                "channel_id": channel_id,
                "title": title,
                "youtube_video_id": youtube_video_id,
            }
        ),
    )


def publish_progress_updated(
    video_id: str,
    user_id: str,
    position_seconds: int,
    is_watched: bool,
) -> None:
    """Publish progress_updated event when watch progress is saved (sync).

    Lets a user's other devices follow along in near-real time.
    """
    _get_sync_redis().publish(
        PROGRESS_CHANNEL,
        json.dumps(
            {
                "type": "progress_updated",
                "video_id": video_id,
                "user_id": user_id,
                "position_seconds": position_seconds,
                "is_watched": is_watched,
            }
        ),
    )


async def _dispatch_event(payload: dict) -> None:
    """Translate one pub/sub payload into a WebSocket frame and broadcast it.

    Factored out of the Redis loop so every event type stays unit-testable.
    """
    from app.api.websocket import broadcast_to_user

    user_id = payload["user_id"]
    msg_type = payload.get("type")

    if msg_type == "preview_ready":
        await broadcast_to_user(
            user_id,
            {"type": "preview_ready", "data": {"video_id": payload["video_id"]}},
        )
    elif msg_type in ("download_complete", "new_episode"):
        # Both carry the same optional channel/title/youtube fields.
        data = {"video_id": payload["video_id"]}
        for key in ("channel_id", "title", "youtube_video_id"):
            if payload.get(key):
                data[key] = payload[key]
        await broadcast_to_user(user_id, {"type": msg_type, "data": data})
    elif msg_type == "progress_updated":
        await broadcast_to_user(
            user_id,
            {
                "type": "progress_updated",
                "data": {
                    "video_id": payload["video_id"],
                    "position_seconds": payload["position_seconds"],
                    "is_watched": payload["is_watched"],
                },
            },
        )
    else:
        # Default: download_progress (backward compatible)
        await broadcast_to_user(
            user_id,
            {
                "type": "download_progress",
                "data": {
                    "video_id": payload["video_id"],
                    "percentage": payload["percentage"],
                },
            },
        )


async def start_progress_listener() -> None:
    """Subscribe to the progress channel and forward events via WebSocket."""
    r = aioredis.from_url(settings.redis_url)
    pubsub = r.pubsub()
    await pubsub.subscribe(PROGRESS_CHANNEL)

    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                await _dispatch_event(json.loads(message["data"]))
            except Exception:
                logger.exception("Error processing progress message")
    except asyncio.CancelledError:
        pass
    finally:
        await pubsub.unsubscribe(PROGRESS_CHANNEL)
        await r.aclose()
