import logging
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.user_video_ref import UserVideoRef
from app.models.video import Video

logger = logging.getLogger(__name__)


async def check_and_delete_orphan(video_id: str, db: AsyncSession) -> bool:
    """
    Check if a video has any remaining active references.
    If not, delete the physical file from disk and clean up thumbnails.
    Returns True if the file was deleted.
    """
    # Count active (non-removed) references
    result = await db.execute(
        select(UserVideoRef).where(
            UserVideoRef.video_id == video_id,
            UserVideoRef.removed_at.is_(None),
        )
    )
    active_refs = result.scalars().all()

    if active_refs:
        return False

    # No active references remain; delete from disk.
    video_result = await db.execute(select(Video).where(Video.id == video_id))
    video = video_result.scalar_one_or_none()
    if not video:
        return False

    # Delete media file
    if video.file_path:
        full_path = video.file_path
        if not os.path.isabs(full_path):
            full_path = os.path.join(settings.media_path, full_path)
        if os.path.exists(full_path):
            try:
                os.remove(full_path)
                logger.info("Deleted orphaned media file: %s", full_path)
            except OSError:
                logger.exception("Failed to delete media file: %s", full_path)

    # Delete thumbnail
    thumb_path = os.path.join(settings.thumbnails_path, f"{video.youtube_video_id}.jpg")
    if os.path.exists(thumb_path):
        try:
            os.remove(thumb_path)
        except OSError:
            logger.exception("Failed to delete thumbnail: %s", thumb_path)

    # Delete the info JSON if it exists
    if video.file_path:
        info_json = os.path.splitext(
            os.path.join(settings.media_path, video.file_path)
        )[0] + ".info.json"
        if os.path.exists(info_json):
            try:
                os.remove(info_json)
            except OSError:
                pass

    logger.info(
        "Orphan cleanup complete for video %s (%s)",
        video_id,
        video.youtube_video_id,
    )
    return True
