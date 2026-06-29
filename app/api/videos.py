import asyncio
import logging
import os
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.auth import get_current_user, validate_token
from app.config import settings
from app.database import get_db
from app.models.channel import Channel
from app.models.user import User
from app.models.user_video_ref import (
    REF_KIND_CACHE,
    REF_KIND_LIBRARY,
    UserVideoRef,
)
from app.models.video import Video
from app.schemas.ticket import AccessTicket
from app.schemas.video import (
    DownloadRequest,
    VideoDetail,
    VideoOut,
    VideoProgress,
    VideoSearchPage,
)
from app.services.instant_stream import (
    InstantStreamError,
    resolve_progressive_url,
    stream_proxy,
)
from app.services.media_server import build_media_response
from app.services.progress_broadcaster import publish_progress_updated
from app.services.storage import check_and_delete_orphan
from app.tasks.download_tasks import download_preview_task, download_video_task
from app.utils.pagination import decode_cursor, encode_cursor
from app.utils.search import escape_like
from app.utils.tickets import (
    SCOPE_STREAM,
    STREAM_TICKET_TTL_SECONDS,
    TicketError,
    mint_ticket,
    verify_ticket,
)
from app.utils.time import utcnow_naive

router = APIRouter(prefix="/api/videos", tags=["videos"])
logger = logging.getLogger(__name__)


async def _ensure_active_ref(
    db: AsyncSession,
    user_id: str,
    video_id: str,
    kind: str = REF_KIND_LIBRARY,
) -> None:
    """Register (or reactivate) the caller's claim on a video.

    The set of active (``removed_at IS NULL``) UserVideoRefs is the download's
    reference count, so any endpoint that expresses "I want this video" must
    leave the caller holding an active ref. Idempotent: creates the ref if
    missing and clears ``removed_at`` if it was soft-deleted.

    ``kind`` records *why* the user holds it. A LIBRARY claim (explicit download)
    **promotes** an existing CACHE ref to LIBRARY, so a video you cold-watched
    and then chose to download joins your collection. A CACHE claim (implicit,
    from playing) never *downgrades* an existing LIBRARY ref — it only sets the
    kind when creating the row.
    """
    now = utcnow_naive()
    set_: dict = {"removed_at": None}
    if kind == REF_KIND_LIBRARY:
        set_["kind"] = REF_KIND_LIBRARY
    stmt = (
        sqlite_insert(UserVideoRef)
        .values(
            user_id=user_id,
            video_id=video_id,
            added_at=now,
            removed_at=None,
            kind=kind,
        )
        .on_conflict_do_update(
            index_elements=["user_id", "video_id"],
            set_=set_,
        )
    )
    await db.execute(stmt)
    await db.commit()


@router.get("", response_model=VideoSearchPage)
async def search_videos(
    q: str | None = Query(None, description="Match video title or channel name"),
    status: str | None = Query(None, description="Exact video status"),
    watched: bool | None = Query(None, description="Filter by watched state"),
    channel_id: str | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VideoSearchPage:
    """Search the caller's video library by title and/or channel name.

    Scoped to the user's active refs (like the feed endpoints), so results are
    only videos the user holds. An empty/absent ``q`` lists the whole library
    (subject to the other filters). Ordered newest-first with cursor pagination.
    """
    sort_key = func.coalesce(Video.uploaded_at, Video.created_at)

    # The library grid is the user's *collection* — LIBRARY refs only. Videos
    # held merely as a play cache never appear here.
    filters = [
        UserVideoRef.user_id == user.id,
        UserVideoRef.removed_at.is_(None),
        UserVideoRef.kind == REF_KIND_LIBRARY,
    ]
    if q and q.strip():
        pattern = f"%{escape_like(q.strip())}%"
        filters.append(
            or_(
                Video.title.ilike(pattern, escape="\\"),
                Channel.name.ilike(pattern, escape="\\"),
            )
        )
    if status:
        filters.append(Video.status == status)
    if watched is not None:
        filters.append(UserVideoRef.is_watched == watched)
    if channel_id:
        filters.append(Video.channel_id == channel_id)

    total = (
        await db.scalar(
            select(func.count())
            .select_from(UserVideoRef)
            .join(Video, UserVideoRef.video_id == Video.id)
            .join(Channel, Video.channel_id == Channel.id)
            .where(*filters)
        )
        or 0
    )

    page_filters = list(filters)
    if cursor:
        decoded = decode_cursor(cursor)
        if decoded is None:
            raise HTTPException(status_code=400, detail="Invalid cursor")
        cur_sort, cur_id = decoded
        page_filters.append(
            or_(sort_key < cur_sort, and_(sort_key == cur_sort, Video.id < cur_id))
        )

    # Fetch one extra row to detect whether another page exists.
    result = await db.execute(
        select(UserVideoRef, Video, Channel)
        .join(Video, UserVideoRef.video_id == Video.id)
        .join(Channel, Video.channel_id == Channel.id)
        .where(*page_filters)
        .order_by(sort_key.desc(), Video.id.desc())
        .limit(limit + 1)
    )
    rows = result.all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    items = [
        VideoOut(
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
            watch_position_seconds=ref.watch_position_seconds,
            is_watched=ref.is_watched,
            last_watched_at=ref.last_watched_at,
            channel_name=channel.name,
        )
        for ref, video, channel in rows
    ]

    next_cursor = None
    if has_more and rows:
        _, last_video, _ = rows[-1]
        next_cursor = encode_cursor(
            last_video.uploaded_at or last_video.created_at, last_video.id
        )

    return VideoSearchPage(items=items, total=total, next_cursor=next_cursor)


@router.get("/downloads", response_model=list[VideoOut])
async def get_active_downloads(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[VideoOut]:
    cutoff = utcnow_naive() - timedelta(seconds=60)

    stmt = (
        select(Video)
        .join(UserVideoRef, UserVideoRef.video_id == Video.id)
        .options(selectinload(Video.channel))
        .where(
            UserVideoRef.user_id == user.id,
            UserVideoRef.removed_at.is_(None),
            # Cache downloads are silent — the Downloads tab tracks the user's
            # collection (LIBRARY) only; a cold-press's background HQ fetch shows
            # up in the player (preview->HQ swap), not here.
            UserVideoRef.kind == REF_KIND_LIBRARY,
            or_(
                Video.status.in_(["PENDING", "DOWNLOADING"]),
                # Include recently completed videos for the "done" transition
                (Video.status == "COMPLETE") & (Video.downloaded_at >= cutoff),
            ),
        )
        .order_by(Video.created_at.desc())
    )
    result = await db.execute(stmt)
    videos = result.scalars().all()

    return [
        VideoOut(
            id=v.id,
            youtube_video_id=v.youtube_video_id,
            channel_id=v.channel_id,
            title=v.title,
            duration_seconds=v.duration_seconds,
            uploaded_at=v.uploaded_at,
            file_size_bytes=v.file_size_bytes or 0,
            status=v.status,
            preview_status=v.preview_status,
            thumbnail_url=f"/data/thumbnails/{v.youtube_video_id}.jpg",
            channel_name=v.channel.name if v.channel else "",
        )
        for v in videos
    ]


@router.get("/{video_id}", response_model=VideoDetail)
async def get_video(
    video_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VideoDetail:
    result = await db.execute(
        select(Video).options(selectinload(Video.channel)).where(Video.id == video_id)
    )
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    ref_result = await db.execute(
        select(UserVideoRef).where(
            UserVideoRef.user_id == user.id,
            UserVideoRef.video_id == video.id,
            UserVideoRef.removed_at.is_(None),
        )
    )
    ref = ref_result.scalar_one_or_none()

    channel = video.channel
    return VideoDetail(
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
        last_watched_at=ref.last_watched_at if ref else None,
        metadata_json=video.metadata_json,
        channel_name=channel.name if channel else "",
        channel_slug=channel.slug if channel else "",
    )


@router.post("/{video_id}/download")
async def trigger_download(
    video_id: str,
    body: DownloadRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    # Ownership: the caller now wants this (shared) video, so register their ref
    # before anything else. This keeps the download ref-counted symmetrically
    # with cancel, and attaches the caller to a download another user may have
    # already started. Committed up front so it survives the 409 paths below.
    await _ensure_active_ref(db, user.id, video_id)

    if video.status in ("PENDING", "DOWNLOADING"):
        raise HTTPException(status_code=409, detail="Download already in progress")
    if video.status == "CANCELLING":
        raise HTTPException(
            status_code=409, detail="Previous download is still being cancelled"
        )

    # CATALOGED, FAILED, COMPLETE — (re-)enqueue. Keep file_path/file_size_bytes
    # intact so the existing file stays playable until the worker replaces it.
    video.status = "PENDING"
    await db.commit()

    quality = body.quality if body else None
    download_video_task.delay(video_id, user.id, quality=quality)

    return {"detail": "Download enqueued", "video_id": video_id}


@router.post("/{video_id}/cache")
async def cache_video(
    video_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Cache a video the user is about to watch (#86).

    Called when a client starts instant playback of a not-yet-downloaded video:
    it records an evictable CACHE claim and, if nothing is downloading it yet,
    enqueues the HQ download so the player can swap preview -> HQ. Idempotent and
    best-effort — unlike ``/download`` it never 409s, because caching is implicit
    (the user pressed play, not "download"). A CACHE claim never downgrades an
    existing LIBRARY ref, and the download is hidden from the Downloads tab.
    """
    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    await _ensure_active_ref(db, user.id, video_id, kind=REF_KIND_CACHE)

    # Already downloaded or in flight (incl. a teardown settling): the player
    # gets HQ from the existing file/download, so there is nothing to enqueue.
    if video.status in ("PENDING", "DOWNLOADING", "COMPLETE", "CANCELLING"):
        return {"status": video.status, "video_id": video_id}

    # CATALOGED or FAILED: kick off the HQ download that backs the cache.
    video.status = "PENDING"
    await db.commit()
    download_video_task.delay(video_id, user.id)

    return {"status": "PENDING", "video_id": video_id}


@router.post("/{video_id}/cancel")
async def cancel_download(
    video_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    # Escape hatch (also covered by the reaper): cancelling a row that is
    # already CANCELLING force-clears a teardown whose worker never confirmed it
    # (e.g. the worker died mid-cancel). This is a recovery action, independent
    # of ref-counting.
    if video.status == "CANCELLING":
        video.status = "CATALOGED"
        await db.commit()
        return {"detail": "Download cancelled", "video_id": video_id}

    # Drop ONLY the caller's intent — never another user's. The shared download
    # is ref-counted by the set of active UserVideoRefs.
    ref_result = await db.execute(
        select(UserVideoRef).where(
            UserVideoRef.user_id == user.id,
            UserVideoRef.video_id == video_id,
            UserVideoRef.removed_at.is_(None),
        )
    )
    ref = ref_result.scalar_one_or_none()
    if ref is not None:
        ref.removed_at = utcnow_naive()
        await db.commit()

    # Not an active download? Nothing to interrupt, but dropping the ref may have
    # orphaned an existing file — reuse the orphan check to remove it if so.
    if video.status not in ("PENDING", "DOWNLOADING"):
        await check_and_delete_orphan(video_id, db)
        return {"detail": "Not in progress", "video_id": video_id}

    # Only tear down the shared download when nobody is left who wants it. This
    # also covers the scheduler: it downloads on subscribers' behalf, and those
    # subscribers hold the refs, so their refs keep the download alive.
    remaining = await db.scalar(
        select(func.count())
        .select_from(UserVideoRef)
        .where(
            UserVideoRef.video_id == video_id,
            UserVideoRef.removed_at.is_(None),
        )
    )
    if remaining and remaining > 0:
        return {
            "detail": "Cancelled for you; download continues for others",
            "video_id": video_id,
            "stopped": False,
        }

    # No active refs remain: truly cancel the shared download.
    if video.status == "PENDING":
        # Never started — the worker's start guard skips a CATALOGED row.
        video.status = "CATALOGED"
    else:  # DOWNLOADING
        # Hand off to the worker: its cancel_check kills yt-dlp, cleans up
        # partial files, then confirms CANCELLING -> CATALOGED. Blocks a
        # concurrent re-download until the teardown completes.
        video.status = "CANCELLING"
    await db.commit()

    return {"detail": "Download cancelled", "video_id": video_id, "stopped": True}


@router.post("/{video_id}/preview")
async def request_preview(
    video_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    if video.status == "COMPLETE":
        return {"preview_status": None, "detail": "HQ already complete"}

    if video.preview_status in ("DOWNLOADING", "READY"):
        return {"preview_status": video.preview_status}

    download_preview_task.delay(video_id, user.id)
    return {"preview_status": "DOWNLOADING"}


@router.post("/{video_id}/playback-ticket", response_model=AccessTicket)
async def create_playback_ticket(
    video_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AccessTicket:
    """Mint a short-lived, video+user-scoped ticket for ``/stream`` (#30).

    Session-authenticated; the returned ticket is passed to the stream endpoints
    as ``?ticket=`` instead of leaking the session token into the media URL.
    """
    exists = await db.scalar(select(Video.id).where(Video.id == video_id))
    if not exists:
        raise HTTPException(status_code=404, detail="Video not found")
    ticket, expires_in = mint_ticket(
        SCOPE_STREAM, user.id, video_id=video_id, ttl_seconds=STREAM_TICKET_TTL_SECONDS
    )
    return AccessTicket(ticket=ticket, expires_in=expires_in)


async def _authorize_stream(
    video_id: str,
    ticket: str | None,
    token: str | None,
    x_user_token: str | None,
    db: AsyncSession,
) -> None:
    """Authorize a media stream request, raising 401 if it cannot be.

    A short-lived playback ticket (``?ticket=``) is checked first, then we fall
    back to the legacy session token carried as ``?token=`` or the X-User-Token
    header. The fallback is kept during the transition so existing clients keep
    working; new clients mint a per-video ticket and never put the session token
    in the URL (#30).
    """
    if ticket:
        try:
            verify_ticket(ticket, scope=SCOPE_STREAM, video_id=video_id)
            return
        except TicketError:
            pass  # fall back to the session token below
    auth_token = token or x_user_token
    if not auth_token or not await validate_token(auth_token):
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/{video_id}/preview-stream")
async def stream_preview(
    video_id: str,
    ticket: str | None = None,
    token: str | None = None,
    x_user_token: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
    range_header: str | None = Header(None, alias="Range"),
) -> Response:
    await _authorize_stream(video_id, ticket, token, x_user_token, db)

    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.preview_status != "READY" or not video.preview_file_path:
        raise HTTPException(status_code=404, detail="Preview not available")

    file_path = video.preview_file_path
    if not os.path.isabs(file_path):
        file_path = os.path.join(settings.media_path, file_path)

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Preview file missing from disk")

    return build_media_response(file_path, range_header)


@router.get("/{video_id}/stream")
async def stream_video(
    video_id: str,
    ticket: str | None = None,
    token: str | None = None,
    x_user_token: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
    range_header: str | None = Header(None, alias="Range"),
) -> Response:
    # Accept auth via a short-lived playback ticket or the session token (query
    # param for the <video> element, or X-User-Token header).
    await _authorize_stream(video_id, ticket, token, x_user_token, db)

    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.status != "COMPLETE" or not video.file_path:
        raise HTTPException(status_code=404, detail="Video file not available")

    file_path = video.file_path
    if not os.path.isabs(file_path):
        file_path = os.path.join(settings.media_path, file_path)

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Video file missing from disk")

    return build_media_response(file_path, range_header)


@router.get("/{video_id}/instant-stream")
async def instant_stream(
    video_id: str,
    ticket: str | None = None,
    token: str | None = None,
    x_user_token: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
    range_header: str | None = Header(None, alias="Range"),
) -> Response:
    """Stream something playable *right now* for a not-yet-downloaded video (#85).

    Removes the cold-press wait: instead of generating and waiting on a whole
    preview file, the backend resolves a progressive source URL and proxies it,
    so playback starts as the first bytes arrive (~1-2s). The HQ download and
    the seamless in-player swap are unchanged and handled separately.

    If a full HQ file already exists we serve that directly — strictly better,
    and it avoids a needless upstream round-trip; otherwise we proxy the source.
    """
    await _authorize_stream(video_id, ticket, token, x_user_token, db)

    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    # Defensive fast path: a client may hit this for a video that already
    # finished downloading; serve the local file rather than proxying the source.
    if video.status == "COMPLETE" and video.file_path:
        file_path = video.file_path
        if not os.path.isabs(file_path):
            file_path = os.path.join(settings.media_path, file_path)
        if os.path.exists(file_path):
            return build_media_response(file_path, range_header)

    try:
        url = await asyncio.to_thread(resolve_progressive_url, video.youtube_video_id)
        return await stream_proxy(url, range_header)
    except InstantStreamError as exc:
        logger.warning("instant-stream resolve/proxy failed for %s: %s", video_id, exc)
        raise HTTPException(
            status_code=502, detail="Could not start instant stream"
        ) from exc


@router.put("/{video_id}/progress")
async def update_progress(
    video_id: str,
    body: VideoProgress,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(Video.duration_seconds).where(Video.id == video_id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Video not found")
    duration = row[0]

    # The client only reports positions; watched state is derived here. A
    # position within the last 5% (or 30s) of the video marks it watched;
    # restarting a watched video makes it in-progress again.
    is_watched = bool(body.is_watched)
    if not is_watched and duration and duration > 0:
        is_watched = body.position_seconds >= max(duration * 0.95, duration - 30)

    now = utcnow_naive()
    stmt = sqlite_insert(UserVideoRef).values(
        user_id=user.id,
        video_id=video_id,
        watch_position_seconds=body.position_seconds,
        is_watched=is_watched,
        added_at=now,
        removed_at=None,
        last_watched_at=now,
        # Watching a video the user hasn't downloaded creates an evictable CACHE
        # ref, never a library entry — playing must not silently build a
        # collection. The conflict path below leaves ``kind`` untouched, so an
        # existing LIBRARY ref is never downgraded by watching it.
        kind=REF_KIND_CACHE,
    )
    # UPSERT: update progress and reactivate soft-deleted refs in one statement.
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "video_id"],
        set_={
            "watch_position_seconds": body.position_seconds,
            "is_watched": is_watched,
            "removed_at": None,
            "last_watched_at": now,
        },
    )
    await db.execute(stmt)
    await db.commit()

    # Live-sync the user's other devices. Best-effort and off the event loop:
    # a publish failure (e.g. Redis down) must never fail the progress save.
    try:
        await asyncio.to_thread(
            publish_progress_updated,
            video_id,
            user.id,
            body.position_seconds,
            is_watched,
        )
    except Exception:
        logger.debug("progress_updated publish failed for video %s", video_id)

    return {"detail": "Progress updated"}


@router.delete("/{video_id}")
async def remove_video_ref(
    video_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(UserVideoRef).where(
            UserVideoRef.user_id == user.id,
            UserVideoRef.video_id == video_id,
            UserVideoRef.removed_at.is_(None),
        )
    )
    ref = result.scalar_one_or_none()
    if not ref:
        raise HTTPException(status_code=404, detail="Video reference not found")

    ref.removed_at = utcnow_naive()
    await db.commit()

    # Check if this was the last active reference; if so, delete file from disk.
    await check_and_delete_orphan(video_id, db)

    return {"detail": "Video reference removed"}
