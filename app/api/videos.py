import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.database import get_db
from app.models.user import User
from app.models.user_video_ref import UserVideoRef
from app.models.video import Video
from app.schemas.video import VideoDetail, VideoProgress
from app.services.media_server import build_range_response
from app.services.storage import check_and_delete_orphan

router = APIRouter(prefix="/api/videos", tags=["videos"])


@router.get("/{video_id}", response_model=VideoDetail)
async def get_video(
    video_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VideoDetail:
    result = await db.execute(select(Video).where(Video.id == video_id))
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
        file_size_bytes=video.file_size_bytes,
        status=video.status,
        thumbnail_url=f"/data/thumbnails/{video.youtube_video_id}.jpg",
        watch_position_seconds=ref.watch_position_seconds if ref else 0,
        is_watched=ref.is_watched if ref else False,
        metadata_json=video.metadata_json,
        channel_name=channel.name if channel else "",
        channel_slug=channel.slug if channel else "",
    )


@router.get("/{video_id}/stream")
async def stream_video(
    video_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    range_header: str | None = Header(None, alias="Range"),
):
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
        raise HTTPException(status_code=404, detail="Video reference not found")

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
