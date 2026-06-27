import asyncio
import logging
import os
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.auth import get_current_user, validate_token
from app.config import settings
from app.database import get_db
from app.models.user import User
from app.models.user_video_ref import UserVideoRef
from app.models.video import Video
from app.schemas.video import DownloadRequest, VideoDetail, VideoOut, VideoProgress
from app.services.media_server import build_media_response
from app.services.progress_broadcaster import publish_progress_updated
from app.services.storage import check_and_delete_orphan
from app.tasks.download_tasks import download_preview_task, download_video_task
from app.utils.time import utcnow_naive

router = APIRouter(prefix="/api/videos", tags=["videos"])
logger = logging.getLogger(__name__)


async def _ensure_active_ref(db: AsyncSession, user_id: str, video_id: str) -> None:
    """Register (or reactivate) the caller's claim on a video.

    The set of active (``removed_at IS NULL``) UserVideoRefs is the download's
    reference count, so any endpoint that expresses "I want this video" must
    leave the caller holding an active ref. Idempotent: creates the ref if
    missing, clears ``removed_at`` if it was soft-deleted, and leaves an
    already-active ref untouched.
    """
    now = utcnow_naive()
    stmt = (
        sqlite_insert(UserVideoRef)
        .values(user_id=user_id, video_id=video_id, added_at=now, removed_at=None)
        .on_conflict_do_update(
            index_elements=["user_id", "video_id"],
            set_={"removed_at": None},
        )
    )
    await db.execute(stmt)
    await db.commit()


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


@router.get("/{video_id}/preview-stream")
async def stream_preview(
    video_id: str,
    token: str | None = None,
    x_user_token: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
    range_header: str | None = Header(None, alias="Range"),
) -> Response:
    auth_token = token or x_user_token
    if not auth_token or not await validate_token(auth_token):
        raise HTTPException(status_code=401, detail="Unauthorized")

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
    token: str | None = None,
    x_user_token: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
    range_header: str | None = Header(None, alias="Range"),
) -> Response:
    # Accept auth via query param (for <video> element) or header
    auth_token = token or x_user_token
    if not auth_token or not await validate_token(auth_token):
        raise HTTPException(status_code=401, detail="Unauthorized")

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
