import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.database import get_db
from app.models.channel import Channel
from app.models.subscription import UserSubscription
from app.models.user import User
from app.models.user_video_ref import UserVideoRef
from app.models.video import Video
from app.schemas.channel import ChannelDetail, ChannelOut, ChannelSubscribe
from app.schemas.video import VideoOut, VideoPagination
from app.services.download_manager import fetch_channel_images, fetch_channel_metadata
from app.tasks.download_tasks import poll_channel_task

router = APIRouter(prefix="/api/channels", tags=["channels"])


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "channel"


@router.get("", response_model=list[ChannelOut])
async def list_channels(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ChannelOut]:
    result = await db.execute(select(Channel).order_by(Channel.name))
    channels = result.scalars().all()

    # Gather subscription status for this user
    sub_result = await db.execute(
        select(UserSubscription.channel_id).where(UserSubscription.user_id == user.id)
    )
    subscribed_ids = {row[0] for row in sub_result.all()}

    out = []
    for ch in channels:
        video_count_result = await db.execute(
            select(func.count()).select_from(Video).where(Video.channel_id == ch.id)
        )
        video_count = video_count_result.scalar() or 0
        item = ChannelOut.model_validate(ch)
        item.video_count = video_count
        item.is_subscribed = ch.id in subscribed_ids
        out.append(item)
    return out


@router.post("/subscribe", response_model=ChannelOut)
async def subscribe(
    body: ChannelSubscribe,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChannelOut:
    yt_channel_id = body.youtube_channel_id

    # Extract channel ID from URL if provided
    if body.url and not yt_channel_id:
        yt_channel_id = _extract_channel_id(body.url)
    if not yt_channel_id:
        raise HTTPException(status_code=400, detail="Provide url or youtube_channel_id")

    # Resolve channel metadata to get canonical UC ID and display name.
    # This lets us detect duplicates when subscribing via handle vs UC ID.
    meta = await _resolve_channel(yt_channel_id)
    canonical_id = meta.get("channel_id", yt_channel_id)
    resolved_name = meta.get("name", yt_channel_id)

    # Check if channel already exists (match either the input ID or canonical UC ID)
    result = await db.execute(
        select(Channel).where(
            Channel.youtube_channel_id.in_([yt_channel_id, canonical_id])
        )
    )
    channel = result.scalar_one_or_none()

    if not channel:
        # Fetch channel avatar & banner from YouTube
        images = await _resolve_channel_images(canonical_id)

        # Create the channel record with resolved metadata
        channel = Channel(
            id=str(uuid.uuid4()),
            youtube_channel_id=canonical_id,
            name=resolved_name,
            slug=_slugify(
                resolved_name if resolved_name != yt_channel_id else yt_channel_id
            ),
            description=meta.get("description", ""),
            avatar_url=images.get("avatar_url"),
            banner_url=images.get("banner_url"),
        )
        db.add(channel)
        await db.flush()

    # Check for existing subscription
    sub_result = await db.execute(
        select(UserSubscription).where(
            UserSubscription.user_id == user.id,
            UserSubscription.channel_id == channel.id,
        )
    )
    existing_sub = sub_result.scalar_one_or_none()
    if existing_sub:
        raise HTTPException(status_code=409, detail="Already subscribed")

    sub = UserSubscription(
        user_id=user.id,
        channel_id=channel.id,
        retention_policy=body.retention_policy,
        retention_count=body.retention_count,
        tracking_mode=body.tracking_mode,
    )
    db.add(sub)

    # Create user video refs for ALL existing videos in this channel (not just COMPLETE)
    video_result = await db.execute(select(Video).where(Video.channel_id == channel.id))
    existing_videos = video_result.scalars().all()
    for video in existing_videos:
        ref_check = await db.execute(
            select(UserVideoRef).where(
                UserVideoRef.user_id == user.id,
                UserVideoRef.video_id == video.id,
            )
        )
        if not ref_check.scalar_one_or_none():
            ref = UserVideoRef(user_id=user.id, video_id=video.id)
            db.add(ref)

    await db.commit()
    await db.refresh(channel)

    # Trigger an immediate poll for this channel
    poll_channel_task.delay(channel.id)

    out = ChannelOut.model_validate(channel)
    out.is_subscribed = True
    return out


@router.post("/{channel_id}/refresh-images", response_model=ChannelDetail)
async def refresh_channel_images(
    channel_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChannelDetail:
    """Fetch fresh avatar and banner images from YouTube and update the channel."""
    result = await db.execute(select(Channel).where(Channel.id == channel_id))
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    images = await _resolve_channel_images(channel.youtube_channel_id)
    if images.get("avatar_url"):
        channel.avatar_url = images["avatar_url"]
    if images.get("banner_url"):
        channel.banner_url = images["banner_url"]
    await db.commit()
    await db.refresh(channel)

    sub_count_result = await db.execute(
        select(func.count())
        .select_from(UserSubscription)
        .where(UserSubscription.channel_id == channel_id)
    )
    subscriber_count = sub_count_result.scalar() or 0

    video_count_result = await db.execute(
        select(func.count()).select_from(Video).where(Video.channel_id == channel_id)
    )
    video_count = video_count_result.scalar() or 0

    sub_result = await db.execute(
        select(UserSubscription).where(
            UserSubscription.user_id == user.id,
            UserSubscription.channel_id == channel_id,
        )
    )
    sub = sub_result.scalar_one_or_none()

    detail = ChannelDetail.model_validate(channel)
    detail.subscriber_count = subscriber_count
    detail.video_count = video_count
    detail.is_subscribed = sub is not None
    if sub:
        detail.tracking_mode = sub.tracking_mode
    return detail


@router.delete("/{channel_id}/unsubscribe")
async def unsubscribe(
    channel_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(UserSubscription).where(
            UserSubscription.user_id == user.id,
            UserSubscription.channel_id == channel_id,
        )
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    await db.delete(sub)
    await db.commit()
    return {"detail": "Unsubscribed"}


@router.get("/{channel_id}", response_model=ChannelDetail)
async def get_channel(
    channel_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChannelDetail:
    result = await db.execute(select(Channel).where(Channel.id == channel_id))
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    sub_count_result = await db.execute(
        select(func.count())
        .select_from(UserSubscription)
        .where(UserSubscription.channel_id == channel_id)
    )
    subscriber_count = sub_count_result.scalar() or 0

    video_count_result = await db.execute(
        select(func.count()).select_from(Video).where(Video.channel_id == channel_id)
    )
    video_count = video_count_result.scalar() or 0

    sub_result = await db.execute(
        select(UserSubscription).where(
            UserSubscription.user_id == user.id,
            UserSubscription.channel_id == channel_id,
        )
    )
    sub = sub_result.scalar_one_or_none()

    detail = ChannelDetail.model_validate(channel)
    detail.subscriber_count = subscriber_count
    detail.video_count = video_count
    detail.is_subscribed = sub is not None
    if sub:
        detail.tracking_mode = sub.tracking_mode
    return detail


@router.get("/{channel_id}/videos", response_model=VideoPagination)
async def list_channel_videos(
    channel_id: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VideoPagination:
    # Total count
    total_result = await db.execute(
        select(func.count()).select_from(Video).where(Video.channel_id == channel_id)
    )
    total = total_result.scalar() or 0

    offset = (page - 1) * per_page
    result = await db.execute(
        select(Video)
        .where(Video.channel_id == channel_id)
        .order_by(Video.uploaded_at.desc())
        .offset(offset)
        .limit(per_page)
    )
    videos = result.scalars().all()

    items = []
    for v in videos:
        ref_result = await db.execute(
            select(UserVideoRef).where(
                UserVideoRef.user_id == user.id,
                UserVideoRef.video_id == v.id,
                UserVideoRef.removed_at.is_(None),
            )
        )
        ref = ref_result.scalar_one_or_none()
        item = VideoOut(
            id=v.id,
            youtube_video_id=v.youtube_video_id,
            channel_id=v.channel_id,
            title=v.title,
            duration_seconds=v.duration_seconds,
            uploaded_at=v.uploaded_at,
            file_size_bytes=v.file_size_bytes or 0,
            status=v.status,
            thumbnail_url=f"/data/thumbnails/{v.youtube_video_id}.jpg",
            watch_position_seconds=ref.watch_position_seconds if ref else 0,
            is_watched=ref.is_watched if ref else False,
        )
        items.append(item)

    return VideoPagination(items=items, total=total, page=page, per_page=per_page)


def _extract_channel_id(url: str) -> str | None:
    """Best-effort extraction of a YouTube channel ID from a URL."""
    patterns = [
        r"youtube\.com/channel/([a-zA-Z0-9_-]+)",
        r"youtube\.com/@([a-zA-Z0-9_.-]+)",
        r"youtube\.com/c/([a-zA-Z0-9_.-]+)",
        r"youtube\.com/user/([a-zA-Z0-9_.-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


async def _resolve_channel(yt_channel_id: str) -> dict:
    """Resolve a YouTube channel handle/ID to its canonical metadata.

    Runs the blocking yt-dlp call in a thread to avoid blocking the event loop.
    """
    import asyncio

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch_channel_metadata, yt_channel_id)


async def _resolve_channel_images(yt_channel_id: str) -> dict:
    """Fetch channel avatar and banner URLs from YouTube.

    Runs the blocking HTTP call in a thread to avoid blocking the event loop.
    """
    import asyncio

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch_channel_images, yt_channel_id)
