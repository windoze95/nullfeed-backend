from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.database import get_db
from app.models.channel import Channel
from app.models.subscription import UserSubscription
from app.models.user import User
from app.models.user_video_ref import UserVideoRef
from app.models.video import Video
from app.schemas.channel import ChannelOut
from app.schemas.feed import FeedItem, HomeFeed
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


async def _continue_watching_items(
    user: User, db: AsyncSession, limit: int
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
        .order_by(
            UserVideoRef.last_watched_at.desc().nullslast(),
            UserVideoRef.added_at.desc(),
        )
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


async def _new_episodes_items(
    user: User, db: AsyncSession, limit: int
) -> list[FeedItem]:
    """Newest unwatched download per subscribed channel for this user."""
    # Get user's subscribed channel IDs
    sub_result = await db.execute(
        select(UserSubscription.channel_id).where(UserSubscription.user_id == user.id)
    )
    subscribed_ids = [row[0] for row in sub_result.all()]
    if not subscribed_ids:
        return []

    # Rank each unwatched, completed video within its channel so the dedup-to-
    # newest-per-channel happens in SQL. The window function runs over the
    # already-filtered set (this user's unwatched, non-removed, COMPLETE videos
    # in subscribed channels), so rank 1 is the newest *unwatched* per channel.
    # Pushing this into the DB keeps latency tied to ``limit`` rather than the
    # size of the subscribed library (the old code loaded every such row and
    # deduped in Python).
    ranked = (
        select(
            Video.id.label("video_id"),
            func.row_number()
            .over(
                partition_by=Video.channel_id,
                order_by=(Video.uploaded_at.desc(), Video.id.desc()),
            )
            .label("rn"),
        )
        .join(UserVideoRef, UserVideoRef.video_id == Video.id)
        .where(
            UserVideoRef.user_id == user.id,
            UserVideoRef.removed_at.is_(None),
            UserVideoRef.is_watched == False,  # noqa: E712
            Video.channel_id.in_(subscribed_ids),
            Video.status == "COMPLETE",
        )
        .subquery()
    )

    result = await db.execute(
        select(UserVideoRef, Video, Channel)
        .join(Video, UserVideoRef.video_id == Video.id)
        .join(Channel, Video.channel_id == Channel.id)
        .join(ranked, ranked.c.video_id == Video.id)
        .where(
            UserVideoRef.user_id == user.id,
            ranked.c.rn == 1,
        )
        .order_by(Video.uploaded_at.desc().nullslast())
        .limit(limit)
    )

    return [
        FeedItem(
            channel=_channel_out(channel),
            video=_video_out(video, ref),
        )
        for ref, video, channel in result.all()
    ]


async def _recently_added_items(
    user: User, db: AsyncSession, limit: int
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
        .order_by(
            Video.downloaded_at.desc().nullslast(),
            Video.created_at.desc(),
        )
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


@router.get("/continue-watching", response_model=list[FeedItem])
async def continue_watching(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=50),
) -> list[FeedItem]:
    """Videos with partial progress, ordered by most recently watched."""
    return await _continue_watching_items(user, db, limit)


@router.get("/new-episodes", response_model=list[FeedItem])
async def new_episodes(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=50),
) -> list[FeedItem]:
    """Channels that have unwatched downloads for this user."""
    return await _new_episodes_items(user, db, limit)


@router.get("/recently-added", response_model=list[FeedItem])
async def recently_added(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=50),
) -> list[FeedItem]:
    """Chronological list of newly downloaded videos across subscribed channels."""
    return await _recently_added_items(user, db, limit)


@router.get("/home", response_model=HomeFeed)
async def home_feed(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=50),
) -> HomeFeed:
    """Unified home payload: all three feed sections in a single round-trip.

    Reuses the exact per-section query helpers, so it stays in lockstep with
    the individual endpoints (which existing clients keep using).
    """
    return HomeFeed(
        continue_watching=await _continue_watching_items(user, db, limit),
        new_episodes=await _new_episodes_items(user, db, limit),
        recently_added=await _recently_added_items(user, db, limit),
    )
