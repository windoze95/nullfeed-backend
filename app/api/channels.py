import asyncio
import logging
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
from app.models.user_video_ref import REF_KIND_CACHE, UserVideoRef
from app.models.video import Video
from app.schemas.channel import (
    BulkSubscribeItem,
    BulkSubscribeItemResult,
    BulkSubscribeRequest,
    BulkSubscribeResponse,
    ChannelDetail,
    ChannelOut,
    ChannelSubscribe,
    ContentFilterUpdate,
)
from app.schemas.video import VideoOut, VideoPagination
from app.services.channel_poller import poll_single_channel
from app.services.download_manager import fetch_channel_images, fetch_channel_metadata
from app.utils.content_type import (
    ALL_CONTENT_TYPES,
    REGULAR,
    effective_hidden_content_types,
)
from app.tasks.download_tasks import (
    _get_sync_db,
    download_video_task,
    poll_all_channels_task,
    poll_channel_task,
)
from app.utils.search import escape_like

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/channels", tags=["channels"])


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "channel"


async def _unique_slug(base: str, db: AsyncSession) -> str:
    """Return `base`, deduped with a -2, -3, ... suffix against existing slugs."""
    result = await db.execute(select(Channel.slug).where(Channel.slug.like(f"{base}%")))
    existing = {row[0] for row in result.all()}
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


async def _ensure_refs_for_channel(
    user_id: str, channel_id: str, db: AsyncSession
) -> None:
    """Ensure the user has an active UserVideoRef for every video in the channel.

    Creates missing refs and reactivates soft-deleted ones (removed_at set).
    """
    video_result = await db.execute(
        select(Video.id).where(Video.channel_id == channel_id)
    )
    video_ids = [row[0] for row in video_result.all()]
    if not video_ids:
        return
    ref_result = await db.execute(
        select(UserVideoRef).where(
            UserVideoRef.user_id == user_id,
            UserVideoRef.video_id.in_(video_ids),
        )
    )
    refs_by_video = {ref.video_id: ref for ref in ref_result.scalars().all()}
    # Following a channel quietly caches its episodes (so they open instantly) —
    # it is not a user-managed "download/library". Refs are CACHE; the cache
    # reaper leaves followed-channel videos alone (per-subscription retention
    # bounds them instead), so they persist while subscribed.
    for video_id in video_ids:
        ref = refs_by_video.get(video_id)
        if ref is None:
            db.add(
                UserVideoRef(user_id=user_id, video_id=video_id, kind=REF_KIND_CACHE)
            )
        elif ref.removed_at is not None:
            ref.removed_at = None


@router.get("", response_model=list[ChannelOut])
async def list_channels(
    q: str | None = Query(
        None, description="Filter by channel name (case-insensitive)"
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ChannelOut]:
    stmt = select(Channel)
    if q and q.strip():
        pattern = f"%{escape_like(q.strip())}%"
        stmt = stmt.where(Channel.name.ilike(pattern, escape="\\"))
    result = await db.execute(stmt.order_by(Channel.name))
    channels = result.scalars().all()

    # Per-channel video counts in a single GROUP BY query
    count_result = await db.execute(
        select(Video.channel_id, func.count()).group_by(Video.channel_id)
    )
    video_counts = {row[0]: row[1] for row in count_result.all()}

    # Gather subscription status for this user
    sub_result = await db.execute(
        select(UserSubscription.channel_id).where(UserSubscription.user_id == user.id)
    )
    subscribed_ids = {row[0] for row in sub_result.all()}

    out = []
    for ch in channels:
        item = ChannelOut.model_validate(ch)
        item.video_count = video_counts.get(ch.id, 0)
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
    meta = await asyncio.to_thread(fetch_channel_metadata, yt_channel_id)
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
        images = await asyncio.to_thread(fetch_channel_images, canonical_id)

        # Create the channel record with resolved metadata
        base_slug = _slugify(
            resolved_name if resolved_name != yt_channel_id else yt_channel_id
        )
        channel = Channel(
            id=str(uuid.uuid4()),
            youtube_channel_id=canonical_id,
            name=resolved_name,
            slug=await _unique_slug(base_slug, db),
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

    # Create/reactivate user video refs for ALL existing videos in this channel
    await _ensure_refs_for_channel(user.id, channel.id, db)

    await db.commit()
    await db.refresh(channel)

    # Trigger an immediate poll for this channel
    poll_channel_task.delay(channel.id)

    out = ChannelOut.model_validate(channel)
    out.is_subscribed = True
    return out


@router.post("/subscribe-bulk", response_model=BulkSubscribeResponse)
async def subscribe_bulk(
    body: BulkSubscribeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BulkSubscribeResponse:
    """Subscribe to up to 25 channels at once. Per-item errors don't fail the batch."""
    results: list[BulkSubscribeItemResult] = []
    for item in body.items:
        try:
            results.append(await _subscribe_bulk_item(item, user, db))
        except Exception:
            logger.exception("Bulk subscribe failed for %s", item.youtube_channel_id)
            await db.rollback()
            results.append(
                BulkSubscribeItemResult(
                    youtube_channel_id=item.youtube_channel_id,
                    status="error",
                    detail="Subscription failed",
                )
            )
    return BulkSubscribeResponse(results=results)


async def _subscribe_bulk_item(
    item: BulkSubscribeItem, user: User, db: AsyncSession
) -> BulkSubscribeItemResult:
    yt_channel_id = item.youtube_channel_id.strip()

    result = await db.execute(
        select(Channel).where(Channel.youtube_channel_id == yt_channel_id)
    )
    channel = result.scalar_one_or_none()

    created = False
    if not channel:
        # Create the channel WITHOUT resolving via yt-dlp when a name is
        # provided; the enqueued poll refreshes metadata/images async.
        name = (item.name or "").strip()
        if not name:
            # Best effort: resolve the display name in a thread executor.
            meta = await asyncio.to_thread(fetch_channel_metadata, yt_channel_id)
            name = (meta.get("name") or "").strip()
            # fetch_channel_metadata echoes the input back on failure.
            if not name or (
                name == yt_channel_id
                and meta.get("channel_id") == yt_channel_id
                and not meta.get("handle")
            ):
                return BulkSubscribeItemResult(
                    youtube_channel_id=item.youtube_channel_id,
                    status="error",
                    detail="Could not resolve channel name",
                )
        channel = Channel(
            id=str(uuid.uuid4()),
            youtube_channel_id=yt_channel_id,
            name=name,
            slug=await _unique_slug(_slugify(name), db),
            description="",
            avatar_url=None,
            banner_url=None,
        )
        db.add(channel)
        await db.flush()
        created = True

    sub_result = await db.execute(
        select(UserSubscription).where(
            UserSubscription.user_id == user.id,
            UserSubscription.channel_id == channel.id,
        )
    )
    if sub_result.scalar_one_or_none():
        return BulkSubscribeItemResult(
            youtube_channel_id=item.youtube_channel_id,
            status="already_subscribed",
            channel_id=channel.id,
        )

    db.add(
        UserSubscription(
            user_id=user.id,
            channel_id=channel.id,
            retention_policy="KEEP_ALL",
            tracking_mode="FUTURE_ONLY",
        )
    )
    await _ensure_refs_for_channel(user.id, channel.id, db)
    await db.commit()

    if created:
        poll_channel_task.delay(channel.id)

    return BulkSubscribeItemResult(
        youtube_channel_id=item.youtube_channel_id,
        status="subscribed",
        channel_id=channel.id,
    )


@router.post("/poll")
async def poll_all_channels_now(
    user: User = Depends(get_current_user),
) -> dict:
    """Kick off a background poll of every channel (pull-to-refresh)."""
    try:
        poll_all_channels_task.delay()
    except Exception:
        logger.exception("Could not enqueue poll-all task")
        raise HTTPException(status_code=502, detail="Could not start poll")
    return {"detail": "Poll started"}


@router.post("/{channel_id}/poll")
async def poll_channel_now(
    channel_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Poll one channel synchronously so pull-to-refresh shows new uploads.

    For an already-cataloged channel this is a cheap RSS conditional GET (often
    a 304 with no further work); only genuinely-new uploads fall through to
    yt-dlp. Fast enough to run inline. Auto-download candidates are enqueued
    exactly as the scheduled poll task does.
    """
    result = await db.execute(select(Channel.id).where(Channel.id == channel_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    def _run() -> dict:
        sync_db = _get_sync_db()
        try:
            poll_result = poll_single_channel(channel_id, sync_db)
        finally:
            sync_db.close()
        enqueue_failures = 0
        for video_id in poll_result["auto_download_ids"]:
            try:
                download_video_task.delay(video_id)
            except Exception:
                enqueue_failures += 1
                logger.exception("Could not enqueue auto-download %s", video_id)
        return {
            "detail": "Polled",
            "cataloged": len(poll_result["cataloged_ids"]),
            "auto_downloads": len(poll_result["auto_download_ids"]) - enqueue_failures,
        }

    try:
        return await asyncio.to_thread(_run)
    except Exception:
        logger.exception("Poll failed for channel %s", channel_id)
        raise HTTPException(status_code=502, detail="Poll failed")


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

    images = await asyncio.to_thread(fetch_channel_images, channel.youtube_channel_id)
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
        detail.hidden_content_types = effective_hidden_content_types(
            sub.hidden_content_types
        )
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
        detail.hidden_content_types = effective_hidden_content_types(
            sub.hidden_content_types
        )
    types_result = await db.execute(
        select(func.coalesce(Video.content_type, REGULAR))
        .where(Video.channel_id == channel_id)
        .distinct()
    )
    detail.available_content_types = sorted(t for (t,) in types_result.all())
    return detail


@router.put("/{channel_id}/content-filter", response_model=ChannelDetail)
async def set_content_filter(
    channel_id: str,
    body: ContentFilterUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChannelDetail:
    """Replace the content types this user has hidden for a channel — the
    per-channel filter the client's type menu drives. An empty list clears it.
    """
    # Reject unknown types so a client typo can't create a filter that silently
    # hides nothing (or, mishandled elsewhere, everything).
    unknown = [t for t in body.hidden_content_types if t not in ALL_CONTENT_TYPES]
    if unknown:
        raise HTTPException(
            status_code=422, detail=f"Unknown content types: {', '.join(unknown)}"
        )

    sub_result = await db.execute(
        select(UserSubscription).where(
            UserSubscription.user_id == user.id,
            UserSubscription.channel_id == channel_id,
        )
    )
    sub = sub_result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail="Not subscribed to this channel")

    # Store the explicit set as given (deduped) — including an empty list, which
    # means "show everything" and must override the members-only default, so it's
    # kept distinct from NULL ("never configured", where the default applies).
    sub.hidden_content_types = list(dict.fromkeys(body.hidden_content_types))
    await db.commit()

    return await get_channel(channel_id, user=user, db=db)


async def _hidden_types_for(channel_id: str, user: User, db: AsyncSession) -> list[str]:
    """The content types effectively hidden for a channel — the gate applied to
    its video list. An unconfigured channel (no stored set / not subscribed)
    falls back to the members-only default; an explicit set (even empty) is used
    as-is. See effective_hidden_content_types."""
    result = await db.execute(
        select(UserSubscription.hidden_content_types).where(
            UserSubscription.user_id == user.id,
            UserSubscription.channel_id == channel_id,
        )
    )
    return effective_hidden_content_types(result.scalar_one_or_none())


@router.get("/{channel_id}/videos", response_model=VideoPagination)
async def list_channel_videos(
    channel_id: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    include_hidden: bool = Query(
        False,
        description="Include content types the user has hidden for this channel "
        "(the 'show hidden' reveal). Off by default so the gate applies.",
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VideoPagination:
    # The per-channel content gate: omit hidden types unless the caller is
    # explicitly revealing them. NULL content_type (rows cataloged before the
    # field existed) counts as "regular", so it's hidden only if regular is.
    hidden = [] if include_hidden else await _hidden_types_for(channel_id, user, db)
    filters = [Video.channel_id == channel_id]
    if hidden:
        filters.append(func.coalesce(Video.content_type, REGULAR).notin_(hidden))

    # Total count
    total_result = await db.execute(
        select(func.count()).select_from(Video).where(*filters)
    )
    total = total_result.scalar() or 0

    offset = (page - 1) * per_page
    # Videos cataloged from flat-playlist polls have no upload date until
    # they are downloaded; fall back to catalog time so freshly discovered
    # episodes sort to the top instead of the bottom.
    result = await db.execute(
        select(Video)
        .where(*filters)
        .order_by(func.coalesce(Video.uploaded_at, Video.created_at).desc())
        .offset(offset)
        .limit(per_page)
    )
    videos = result.scalars().all()

    # One query for the user's refs across this page of videos
    refs_by_video: dict[str, UserVideoRef] = {}
    video_ids = [v.id for v in videos]
    if video_ids:
        ref_result = await db.execute(
            select(UserVideoRef).where(
                UserVideoRef.user_id == user.id,
                UserVideoRef.video_id.in_(video_ids),
                UserVideoRef.removed_at.is_(None),
            )
        )
        refs_by_video = {ref.video_id: ref for ref in ref_result.scalars().all()}

    items = []
    for v in videos:
        ref = refs_by_video.get(v.id)
        item = VideoOut(
            id=v.id,
            youtube_video_id=v.youtube_video_id,
            channel_id=v.channel_id,
            title=v.title,
            duration_seconds=v.duration_seconds,
            uploaded_at=v.uploaded_at,
            file_size_bytes=v.file_size_bytes or 0,
            status=v.status,
            preview_status=v.preview_status,
            unplayable_reason=v.unplayable_reason,
            content_type=v.content_type,
            thumbnail_url=f"/data/thumbnails/{v.youtube_video_id}.jpg",
            watch_position_seconds=ref.watch_position_seconds if ref else 0,
            is_watched=ref.is_watched if ref else False,
            last_watched_at=ref.last_watched_at if ref else None,
        )
        items.append(item)

    return VideoPagination(items=items, total=total, page=page, per_page=per_page)


def _extract_channel_id(url: str) -> str | None:
    """Best-effort extraction of a YouTube channel ID from a URL or handle."""
    url = url.strip()

    # Bare handle ("@mkbhd") or raw UC channel id — e.g. from AI
    # recommendations, which store handles for one-tap subscribe.
    if re.fullmatch(r"@[a-zA-Z0-9_.-]+", url):
        return url
    if re.fullmatch(r"UC[a-zA-Z0-9_-]{10,}", url):
        return url

    patterns = [
        r"youtube\.com/channel/([a-zA-Z0-9_-]+)",
        r"youtube\.com/(@[a-zA-Z0-9_.-]+)",
        r"youtube\.com/c/([a-zA-Z0-9_.-]+)",
        r"youtube\.com/user/([a-zA-Z0-9_.-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None
