"""Event-dispatch tests for the progress broadcaster (#10 live events).

These exercise _dispatch_event directly so every event type's WebSocket frame
shape is pinned, including the two new types and the pre-existing ones.
"""

from unittest.mock import AsyncMock

import pytest

import app.api.websocket as websocket_api
from app.services.progress_broadcaster import _dispatch_event

pytestmark = pytest.mark.asyncio


@pytest.fixture
def sent(monkeypatch):
    """Capture broadcast_to_user calls without any real WebSocket."""
    mock = AsyncMock()
    monkeypatch.setattr(websocket_api, "broadcast_to_user", mock)
    return mock


async def test_dispatch_new_episode(sent):
    await _dispatch_event(
        {
            "type": "new_episode",
            "user_id": "u1",
            "video_id": "v1",
            "channel_id": "c1",
            "title": "Episode 1",
            "youtube_video_id": "yt1",
        }
    )
    sent.assert_awaited_once_with(
        "u1",
        {
            "type": "new_episode",
            "data": {
                "video_id": "v1",
                "channel_id": "c1",
                "title": "Episode 1",
                "youtube_video_id": "yt1",
            },
        },
    )


async def test_dispatch_progress_updated(sent):
    await _dispatch_event(
        {
            "type": "progress_updated",
            "user_id": "u1",
            "video_id": "v1",
            "position_seconds": 42,
            "is_watched": False,
        }
    )
    sent.assert_awaited_once_with(
        "u1",
        {
            "type": "progress_updated",
            "data": {"video_id": "v1", "position_seconds": 42, "is_watched": False},
        },
    )


async def test_dispatch_download_complete_still_works(sent):
    await _dispatch_event(
        {
            "type": "download_complete",
            "user_id": "u1",
            "video_id": "v1",
            "channel_id": "c1",
        }
    )
    sent.assert_awaited_once_with(
        "u1",
        {"type": "download_complete", "data": {"video_id": "v1", "channel_id": "c1"}},
    )


async def test_dispatch_download_progress_is_default(sent):
    await _dispatch_event({"user_id": "u1", "video_id": "v1", "percentage": 50.0})
    sent.assert_awaited_once_with(
        "u1",
        {"type": "download_progress", "data": {"video_id": "v1", "percentage": 50.0}},
    )
