"""Seeding and mocking helpers shared across test modules."""

import json
import subprocess
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.channel import Channel
from app.models.subscription import UserSubscription
from app.models.user_video_ref import UserVideoRef
from app.models.video import Video

# A canonical yt-dlp channel-resolve JSON document (flat-playlist, items 0).
IDENTITY_JSON: dict[str, Any] = {
    "channel_id": "UCabc123",
    "channel": "Test Channel",
    "uploader_id": "@testchannel",
    "description": "A test channel",
    "channel_follower_count": 12345,
    "thumbnails": [
        {"id": "avatar_uncropped", "url": "https://img/avatar.jpg"},
        {"id": "banner_uncropped", "url": "https://img/banner.jpg"},
        {"url": "https://img/big-square.jpg", "width": 800, "height": 800},
    ],
}


def fake_completed_process(
    stdout: dict | list | str,
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess:
    """Build a CompletedProcess like a mocked yt-dlp invocation would return."""
    if not isinstance(stdout, str):
        stdout = json.dumps(stdout)
    return subprocess.CompletedProcess(
        args=["yt-dlp"], returncode=returncode, stdout=stdout, stderr=stderr
    )


async def seed_channel(
    db: AsyncSession,
    *,
    name: str = "Test Channel",
    youtube_channel_id: str | None = None,
    slug: str | None = None,
) -> Channel:
    suffix = uuid.uuid4().hex[:8]
    channel = Channel(
        id=str(uuid.uuid4()),
        youtube_channel_id=youtube_channel_id or f"UCtest{suffix}",
        name=name,
        slug=slug or f"test-channel-{suffix}",
        description="",
    )
    db.add(channel)
    await db.commit()
    await db.refresh(channel)
    return channel


async def seed_video(
    db: AsyncSession,
    channel: Channel,
    *,
    title: str = "Test Video",
    status: str = "CATALOGED",
    file_path: str | None = None,
    downloaded_at: datetime | None = None,
    uploaded_at: datetime | None = None,
    preview_file_path: str | None = None,
    preview_status: str | None = None,
) -> Video:
    video = Video(
        id=str(uuid.uuid4()),
        youtube_video_id=f"yt{uuid.uuid4().hex[:9]}",
        channel_id=channel.id,
        title=title,
        status=status,
        file_path=file_path,
        downloaded_at=downloaded_at,
        uploaded_at=uploaded_at,
        preview_file_path=preview_file_path,
        preview_status=preview_status,
    )
    db.add(video)
    await db.commit()
    await db.refresh(video)
    return video


async def seed_ref(
    db: AsyncSession, user_id: str, video_id: str, **kwargs: Any
) -> UserVideoRef:
    ref = UserVideoRef(user_id=user_id, video_id=video_id, **kwargs)
    db.add(ref)
    await db.commit()
    return ref


async def seed_subscription(
    db: AsyncSession, user_id: str, channel_id: str, **kwargs: Any
) -> UserSubscription:
    sub = UserSubscription(user_id=user_id, channel_id=channel_id, **kwargs)
    db.add(sub)
    await db.commit()
    return sub
