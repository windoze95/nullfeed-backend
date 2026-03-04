from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.database import get_db
from app.models.channel import Channel
from app.models.subscription import UserSubscription
from app.models.user import User
from app.models.user_video_ref import UserVideoRef
from app.models.video import Video
from app.schemas.channel import ChannelOut
from app.schemas.feed import FeedItem
from app.schemas.video import VideoOut

router = APIRouter(prefix="/api/feed", tags=["feed"])


def _channel_out(channel: Channel) -> ChannelOut:
    """Build a ChannelOut from an ORM Channel, omitting per-request fields."""
    out = ChannelOut.model_validate(channel)
    return out


def _video_out(video: Video, ref: UserVideoRef | None = None) -> VideoOut:
    return VideoOut(
        id=video.id,
        youtube_video_id=video.youtube_video_id,
        channel_id=video.channel_id,
        title=video.title,
        duration_seconds=video.duration_seconds,
        uploaded_at=video.uploaded_at,
        file_size_bytes=video.file_size_bytes or 0,
        status=video.status,
        preview_status=video.preview_status,
        thumbnail_url=f"/data/thumbnails/{video.youtube_video_id}.jpg",
        watch_position_seconds=ref.watch_position_seconds if ref else 0,
        is_watched=ref.is_watched if ref else False,
    )


@router.get("/continue-watching", response_model=list[FeedItem])
async def continue_watching(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=50),
) -> list[FeedItem]:
    """Videos with partial progress, ordered by most recently watched."""
    result = await db.execute(
        select(UserVideoRef, Video, Channel)
        .join(Video, UserVideoRef.video_id == Video.id)
        .join(Channel, Video.channel_id == Channel.id)
        .where(
            UserVideoRef.user_id == user.id,
            UserVideoRef.removed_at.is_(None),
            UserVideoRef.is_watched == False,  # noqa: E712
            UserVideoRef.watch_position_seconds > 0,
            Video.status == "COMPLETE",
        )
        .order_by(UserVideoRef.added_at.desc())
        .limit(limit)
    )
    rows = result.all()

    items = []
    seen_channels: set[str] = set()
    for ref, video, channel in rows:
        if channel.id in seen_channels:
            continue
        seen_channels.add(channel.id)
        items.append(
            FeedItem(
                channel=_channel_out(channel),
                video=_video_out(video, ref),
            )
        )
    return items


@router.get("/new-episodes", response_model=list[FeedItem])
async def new_episodes(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=50),
) -> list[FeedItem]:
    """Channels that have unwatched downloads for this user."""
    # Get user's subscribed channel IDs
    sub_result = await db.execute(
        select(UserSubscription.channel_id).where(UserSubscription.user_id == user.id)
    )
    subscribed_ids = [row[0] for row in sub_result.all()]

    items = []
    for channel_id in subscribed_ids:
        ch_result = await db.execute(select(Channel).where(Channel.id == channel_id))
        channel = ch_result.scalar_one_or_none()
        if not channel:
            continue

        # Count unwatched videos for this user in this channel
        unwatched_result = await db.execute(
            select(UserVideoRef, Video)
            .join(Video, UserVideoRef.video_id == Video.id)
            .where(
                UserVideoRef.user_id == user.id,
                UserVideoRef.removed_at.is_(None),
                UserVideoRef.is_watched == False,  # noqa: E712
                Video.channel_id == channel_id,
                Video.status == "COMPLETE",
            )
            .order_by(Video.uploaded_at.desc())
        )
        unwatched_rows = unwatched_result.all()
        if not unwatched_rows:
            continue

        ref, latest_video = unwatched_rows[0]
        items.append(
            FeedItem(
                channel=_channel_out(channel),
                video=_video_out(latest_video, ref),
            )
        )

    return items[:limit]


@router.get("/recently-added", response_model=list[FeedItem])
async def recently_added(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=50),
) -> list[FeedItem]:
    """Chronological list of newly downloaded videos across subscribed channels."""
    result = await db.execute(
        select(UserVideoRef, Video, Channel)
        .join(Video, UserVideoRef.video_id == Video.id)
        .join(Channel, Video.channel_id == Channel.id)
        .where(
            UserVideoRef.user_id == user.id,
            UserVideoRef.removed_at.is_(None),
            Video.status == "COMPLETE",
        )
        .order_by(Video.uploaded_at.desc())
        .limit(limit)
    )
    rows = result.all()

    return [
        FeedItem(
            channel=_channel_out(channel),
            video=_video_out(video, ref),
        )
        for ref, video, channel in rows
    ]
