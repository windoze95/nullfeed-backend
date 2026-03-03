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
from app.schemas.feed import ContinueWatchingItem, NewEpisodesItem
from app.schemas.video import VideoOut

router = APIRouter(prefix="/api/feed", tags=["feed"])


@router.get("/continue-watching", response_model=list[ContinueWatchingItem])
async def continue_watching(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=50),
) -> list[ContinueWatchingItem]:
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
            ContinueWatchingItem(
                channel_id=channel.id,
                channel_name=channel.name,
                channel_slug=channel.slug,
                channel_avatar_url=channel.avatar_url,
                video=VideoOut(
                    id=video.id,
                    youtube_video_id=video.youtube_video_id,
                    channel_id=video.channel_id,
                    title=video.title,
                    duration_seconds=video.duration_seconds,
                    uploaded_at=video.uploaded_at,
                    file_size_bytes=video.file_size_bytes,
                    status=video.status,
                    thumbnail_url=f"/data/thumbnails/{video.youtube_video_id}.jpg",
                    watch_position_seconds=ref.watch_position_seconds,
                    is_watched=ref.is_watched,
                ),
            )
        )
    return items


@router.get("/new-episodes", response_model=list[NewEpisodesItem])
async def new_episodes(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=50),
) -> list[NewEpisodesItem]:
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
            NewEpisodesItem(
                channel_id=channel.id,
                channel_name=channel.name,
                channel_slug=channel.slug,
                channel_avatar_url=channel.avatar_url,
                channel_banner_url=channel.banner_url,
                unwatched_count=len(unwatched_rows),
                latest_video=VideoOut(
                    id=latest_video.id,
                    youtube_video_id=latest_video.youtube_video_id,
                    channel_id=latest_video.channel_id,
                    title=latest_video.title,
                    duration_seconds=latest_video.duration_seconds,
                    uploaded_at=latest_video.uploaded_at,
                    file_size_bytes=latest_video.file_size_bytes,
                    status=latest_video.status,
                    thumbnail_url=f"/data/thumbnails/{latest_video.youtube_video_id}.jpg",
                    watch_position_seconds=ref.watch_position_seconds,
                    is_watched=ref.is_watched,
                ),
            )
        )

    return items[:limit]


@router.get("/recently-added", response_model=list[VideoOut])
async def recently_added(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=50),
) -> list[VideoOut]:
    """Chronological list of newly downloaded videos across subscribed channels."""
    result = await db.execute(
        select(UserVideoRef, Video)
        .join(Video, UserVideoRef.video_id == Video.id)
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
        VideoOut(
            id=video.id,
            youtube_video_id=video.youtube_video_id,
            channel_id=video.channel_id,
            title=video.title,
            duration_seconds=video.duration_seconds,
            uploaded_at=video.uploaded_at,
            file_size_bytes=video.file_size_bytes,
            status=video.status,
            thumbnail_url=f"/data/thumbnails/{video.youtube_video_id}.jpg",
            watch_position_seconds=ref.watch_position_seconds,
            is_watched=ref.is_watched,
        )
        for ref, video in rows
    ]
