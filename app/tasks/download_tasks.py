import logging
from datetime import datetime, timezone

from celery import shared_task
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models.channel import Channel
from app.models.video import Video
from app.services.channel_poller import poll_all_channels, poll_single_channel
from app.services.download_manager import download_video

logger = logging.getLogger(__name__)

# Synchronous engine for Celery tasks
_engine = create_engine(settings.sync_database_url, connect_args={"check_same_thread": False})
_SessionLocal = sessionmaker(bind=_engine)


def _get_sync_db() -> Session:
    return _SessionLocal()


@shared_task(
    name="app.tasks.download_tasks.poll_all_channels_task",
    bind=True,
    max_retries=0,
)
def poll_all_channels_task(self) -> dict:
    """Periodic task: poll all subscribed channels for new videos."""
    db = _get_sync_db()
    try:
        poll_all_channels(db)

        # Enqueue downloads for any PENDING videos
        pending_result = db.execute(select(Video).where(Video.status == "PENDING"))
        pending_videos = pending_result.scalars().all()

        enqueued = 0
        for video in pending_videos:
            download_video_task.delay(video.id)
            enqueued += 1

        return {"status": "ok", "enqueued": enqueued}
    except Exception:
        logger.exception("Error in poll_all_channels_task")
        return {"status": "error"}
    finally:
        db.close()


@shared_task(
    name="app.tasks.download_tasks.poll_channel_task",
    bind=True,
    max_retries=0,
)
def poll_channel_task(self, channel_id: str) -> dict:
    """Poll a single channel and enqueue downloads for new videos."""
    db = _get_sync_db()
    try:
        new_video_ids = poll_single_channel(channel_id, db)

        for video_id in new_video_ids:
            download_video_task.delay(video_id)

        return {"status": "ok", "new_videos": len(new_video_ids)}
    except Exception:
        logger.exception("Error polling channel %s", channel_id)
        return {"status": "error"}
    finally:
        db.close()


@shared_task(
    name="app.tasks.download_tasks.download_video_task",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    autoretry_for=(RuntimeError,),
    retry_backoff=True,
    retry_backoff_max=600,
)
def download_video_task(self, video_id: str) -> dict:
    """Download a single video from YouTube."""
    db = _get_sync_db()
    try:
        video = db.get(Video, video_id)
        if not video:
            logger.error("Video %s not found", video_id)
            return {"status": "error", "reason": "not_found"}

        if video.status == "COMPLETE":
            return {"status": "skipped", "reason": "already_complete"}

        channel = db.get(Channel, video.channel_id)
        if not channel:
            logger.error("Channel %s not found for video %s", video.channel_id, video_id)
            return {"status": "error", "reason": "channel_not_found"}

        # Transition to DOWNLOADING
        video.status = "DOWNLOADING"
        db.commit()

        # Perform the download
        result = download_video(
            youtube_video_id=video.youtube_video_id,
            channel_slug=channel.slug,
            quality=settings.media_quality,
        )

        # Update video record with results
        video.file_path = result["file_path"]
        video.file_size_bytes = result["file_size_bytes"]
        video.title = result["title"]
        video.duration_seconds = result["duration_seconds"]
        video.metadata_json = result.get("metadata_json")
        video.status = "COMPLETE"

        if result.get("uploaded_at"):
            try:
                video.uploaded_at = datetime.strptime(
                    result["uploaded_at"], "%Y%m%d"
                ).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass

        db.commit()

        logger.info("Download complete: %s (%s)", video.youtube_video_id, video.title)
        return {"status": "complete", "video_id": video_id}

    except Exception as exc:
        logger.exception("Download failed for video %s", video_id)
        # Mark as FAILED if we've exhausted retries
        try:
            video = db.get(Video, video_id)
            if video and self.request.retries >= self.max_retries:
                video.status = "FAILED"
                db.commit()
        except Exception:
            pass
        raise exc
    finally:
        db.close()
