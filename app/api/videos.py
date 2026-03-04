import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.auth import get_current_user, validate_token
from app.database import get_db
from app.models.user import User
from app.models.user_video_ref import UserVideoRef
from app.models.video import Video
from app.schemas.video import VideoDetail, VideoOut, VideoProgress
from app.services.media_server import build_range_response
from app.services.storage import check_and_delete_orphan
from app.tasks.download_tasks import download_preview_task, download_video_task

router = APIRouter(prefix="/api/videos", tags=["videos"])


@router.get("/downloads", response_model=list[VideoOut])
async def get_active_downloads(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[VideoOut]:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=60)

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
                (Video.status == "COMPLETE") & (Video.created_at >= cutoff),
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
        metadata_json=video.metadata_json,
        channel_name=channel.name if channel else "",
        channel_slug=channel.slug if channel else "",
    )


@router.post("/{video_id}/download")
async def trigger_download(
    video_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    if video.status in ("DOWNLOADING", "PENDING"):
        return {"detail": "Download already in progress", "video_id": video_id}

    # CATALOGED, FAILED, COMPLETE — (re-)enqueue
    video.status = "PENDING"
    video.file_path = None
    video.file_size_bytes = 0
    await db.commit()

    download_video_task.delay(video_id, user.id)

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

    if video.status not in ("PENDING", "DOWNLOADING"):
        return {"detail": "Not in progress", "video_id": video_id}

    video.status = "CATALOGED"
    await db.commit()

    return {"detail": "Download cancelled", "video_id": video_id}


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
):
    auth_token = token or x_user_token
    if not auth_token or not validate_token(auth_token):
        raise HTTPException(status_code=401, detail="Unauthorized")

    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.preview_status != "READY" or not video.preview_file_path:
        raise HTTPException(status_code=404, detail="Preview not available")

    file_path = video.preview_file_path
    if not os.path.isabs(file_path):
        file_path = os.path.join("/data/media", file_path)

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Preview file missing from disk")

    if range_header:
        return build_range_response(file_path, range_header)

    return FileResponse(
        file_path,
        media_type="video/mp4",
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(os.path.getsize(file_path)),
        },
    )


@router.get("/{video_id}/stream")
async def stream_video(
    video_id: str,
    token: str | None = None,
    x_user_token: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
    range_header: str | None = Header(None, alias="Range"),
):
    # Accept auth via query param (for <video> element) or header
    auth_token = token or x_user_token
    if not auth_token or not validate_token(auth_token):
        raise HTTPException(status_code=401, detail="Unauthorized")

    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.status != "COMPLETE" or not video.file_path:
        raise HTTPException(status_code=404, detail="Video file not available")

    file_path = video.file_path
    if not os.path.isabs(file_path):
        file_path = os.path.join("/data/media", file_path)

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Video file missing from disk")

    if range_header:
        return build_range_response(file_path, range_header)

    return FileResponse(
        file_path,
        media_type="video/mp4",
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(os.path.getsize(file_path)),
        },
    )


@router.put("/{video_id}/progress")
async def update_progress(
    video_id: str,
    body: VideoProgress,
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
        ref = UserVideoRef(
            user_id=user.id,
            video_id=video_id,
            watch_position_seconds=body.position_seconds,
            is_watched=body.is_watched,
        )
        db.add(ref)
    else:
        ref.watch_position_seconds = body.position_seconds
        ref.is_watched = body.is_watched
    await db.commit()
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

    ref.removed_at = datetime.now(timezone.utc)
    await db.commit()

    # Check if this was the last active reference; if so, delete file from disk.
    await check_and_delete_orphan(video_id, db)

    return {"detail": "Video reference removed"}
