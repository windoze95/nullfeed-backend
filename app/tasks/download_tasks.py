import logging
import os
from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.database import register_sqlite_pragmas
from app.tasks.celery_app import celery_app
from app.models.channel import Channel
from app.models.subscription import UserSubscription
from app.models.video import Video
from app.services.channel_poller import (
    poll_all_channels,
    poll_single_channel,
    refresh_stale_channel_metadata,
)
from app.services.download_manager import (
    DownloadCancelled,
    download_preview,
    download_video,
)
from app.utils.time import utcnow_naive
from app.services.progress_broadcaster import (
    publish_download_complete,
    publish_download_progress,
    publish_preview_ready,
)

logger = logging.getLogger(__name__)

# Synchronous engine for Celery tasks
_engine = create_engine(
    settings.sync_database_url, connect_args={"check_same_thread": False}
)
register_sqlite_pragmas(_engine)
_SessionLocal = sessionmaker(bind=_engine)


def _get_sync_db() -> Session:
    return _SessionLocal()


@celery_app.task(
    name="app.tasks.download_tasks.poll_all_channels_task",
    bind=True,
    max_retries=0,
)
def poll_all_channels_task(self) -> dict:
    """Periodic task: poll all subscribed channels for new videos."""
    db = _get_sync_db()
    try:
        auto_download_ids = poll_all_channels(db)

        # Only enqueue auto-download candidates (not blanket PENDING sweep)
        enqueued = 0
        for video_id in auto_download_ids:
            download_video_task.delay(video_id)
            enqueued += 1

        return {"status": "ok", "enqueued": enqueued}
    except Exception:
        logger.exception("Error in poll_all_channels_task")
        return {"status": "error"}
    finally:
        db.close()


@celery_app.task(
    name="app.tasks.download_tasks.poll_channel_task",
    bind=True,
    max_retries=0,
)
def poll_channel_task(self, channel_id: str) -> dict:
    """Poll a single channel and enqueue downloads for auto-download candidates."""
    db = _get_sync_db()
    try:
        result = poll_single_channel(channel_id, db)
        auto_download_ids = result["auto_download_ids"]

        for video_id in auto_download_ids:
            download_video_task.delay(video_id)

        return {
            "status": "ok",
            "cataloged": len(result["cataloged_ids"]),
            "auto_downloads": len(auto_download_ids),
        }
    except Exception:
        logger.exception("Error polling channel %s", channel_id)
        return {"status": "error"}
    finally:
        db.close()


@celery_app.task(
    name="app.tasks.download_tasks.refresh_stale_channel_metadata_task",
    bind=True,
    max_retries=0,
)
def refresh_stale_channel_metadata_task(self) -> dict:
    """Periodic task: refresh channel metadata (name, avatar, banner) for stale channels."""
    db = _get_sync_db()
    try:
        updated = refresh_stale_channel_metadata(db)
        return {"status": "ok", "updated": updated}
    except Exception:
        logger.exception("Error in refresh_stale_channel_metadata_task")
        return {"status": "error"}
    finally:
        db.close()


@celery_app.task(
    name="app.tasks.download_tasks.download_video_task",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    autoretry_for=(RuntimeError,),
    retry_backoff=True,
    retry_backoff_max=600,
)
def download_video_task(
    self,
    video_id: str,
    user_id: str | None = None,
    quality: str | None = None,
) -> dict:
    """Download a single video from YouTube."""
    db = _get_sync_db()
    try:
        video = db.get(Video, video_id)
        if not video:
            logger.error("Video %s not found", video_id)
            return {"status": "error", "reason": "not_found"}

        if video.status == "COMPLETE":
            return {"status": "skipped", "reason": "already_complete"}

        # Guard: another worker is already downloading this video
        if video.status == "DOWNLOADING":
            return {"status": "skipped", "reason": "already_downloading"}

        # Guard: a cancel is still being confirmed; finish the hand-off here
        # (the worker that owned the download has already exited).
        if video.status == "CANCELLING":
            video.status = "CATALOGED"
            db.commit()
            return {"status": "skipped", "reason": "cancelled"}

        # Guard: skip CATALOGED videos (they must be explicitly triggered)
        if video.status == "CATALOGED":
            return {"status": "skipped", "reason": "cataloged"}

        channel = db.get(Channel, video.channel_id)
        if not channel:
            logger.error(
                "Channel %s not found for video %s", video.channel_id, video_id
            )
            return {"status": "error", "reason": "channel_not_found"}

        # Remove old file if re-downloading (e.g. codec change)
        if video.file_path:
            old_path = os.path.join(settings.media_path, video.file_path)
            if os.path.exists(old_path):
                os.remove(old_path)
                logger.info("Removed old file for re-download: %s", old_path)

        # Transition to DOWNLOADING
        video.status = "DOWNLOADING"
        db.commit()

        # Build progress callback if we know who triggered the download
        progress_cb = None
        if user_id:

            def progress_cb(percentage: float) -> None:
                publish_download_progress(video_id, user_id, percentage)

        def _is_cancelled() -> bool:
            """True when the video is no longer DOWNLOADING (e.g. cancelled)."""
            check_db = _get_sync_db()
            try:
                status = check_db.execute(
                    select(Video.status).where(Video.id == video_id)
                ).scalar_one_or_none()
                return status != "DOWNLOADING"
            except Exception:
                logger.warning("Cancel check failed for video %s", video_id)
                return False
            finally:
                check_db.close()

        # Perform the download
        result = download_video(
            youtube_video_id=video.youtube_video_id,
            channel_slug=channel.slug,
            quality=quality or settings.media_quality,
            progress_callback=progress_cb,
            cancel_check=_is_cancelled,
        )

        # Update video record with results
        video.file_path = result["file_path"]
        video.file_size_bytes = result["file_size_bytes"]
        video.title = result["title"]
        video.duration_seconds = result["duration_seconds"]
        video.metadata_json = result.get("metadata_json")
        video.status = "COMPLETE"
        video.downloaded_at = utcnow_naive()

        if result.get("uploaded_at"):
            try:
                video.uploaded_at = datetime.strptime(result["uploaded_at"], "%Y%m%d")
            except (ValueError, TypeError):
                pass

        # Clean up preview file now that HQ is ready
        if video.preview_file_path:
            preview_path = os.path.join(settings.media_path, video.preview_file_path)
            if os.path.exists(preview_path):
                try:
                    os.remove(preview_path)
                    logger.info("Removed preview file: %s", preview_path)
                except OSError:
                    logger.warning("Failed to remove preview file: %s", preview_path)
            video.preview_file_path = None
            video.preview_status = None

        db.commit()

        # Notify all subscribers of this channel. Isolated from the retry
        # path: a publish failure after commit must never re-download.
        try:
            subscriber_ids = (
                db.execute(
                    select(UserSubscription.user_id).where(
                        UserSubscription.channel_id == video.channel_id
                    )
                )
                .scalars()
                .all()
            )
            for sub_user_id in subscriber_ids:
                publish_download_complete(
                    video_id,
                    sub_user_id,
                    channel_id=video.channel_id,
                    title=video.title,
                    youtube_video_id=video.youtube_video_id,
                )
        except Exception:
            logger.exception(
                "Failed to publish download_complete for video %s", video_id
            )

        logger.info("Download complete: %s (%s)", video.youtube_video_id, video.title)
        return {"status": "complete", "video_id": video_id}

    except DownloadCancelled:
        # Partial files were cleaned up by the download loop. Confirm the
        # cancel hand-off: CANCELLING -> CATALOGED re-enables re-downloads.
        logger.info("Download cancelled for video %s", video_id)
        try:
            db.rollback()
            video = db.get(Video, video_id)
            if video and video.status == "CANCELLING":
                video.status = "CATALOGED"
                db.commit()
        except Exception:
            logger.exception("Could not confirm cancel for video %s", video_id)
        return {"status": "cancelled", "video_id": video_id}

    except Exception as exc:
        logger.exception("Download failed for video %s", video_id)
        # A retry must pass the already-downloading guard above, so reset the
        # status to PENDING when another attempt is coming; otherwise this is
        # terminal (retries exhausted, or an exception type we never retry)
        # and the video is marked FAILED.
        will_retry = (
            isinstance(exc, RuntimeError) and self.request.retries < self.max_retries
        )
        try:
            db.rollback()
            video = db.get(Video, video_id)
            if video and video.status == "DOWNLOADING":
                video.status = "PENDING" if will_retry else "FAILED"
                db.commit()
        except Exception:
            logger.exception(
                "Could not update status after failed download of %s", video_id
            )
        raise exc
    finally:
        db.close()


@celery_app.task(
    name="app.tasks.download_tasks.download_preview_task",
    bind=True,
    max_retries=1,
    default_retry_delay=10,
    autoretry_for=(RuntimeError,),
)
def download_preview_task(self, video_id: str, user_id: str) -> dict:
    """Download a 360p preview for quick playback while HQ downloads."""
    db = _get_sync_db()
    try:
        video = db.get(Video, video_id)
        if not video:
            logger.error("Video %s not found for preview", video_id)
            return {"status": "error", "reason": "not_found"}

        # Skip if HQ already complete or preview already ready
        if video.status == "COMPLETE":
            return {"status": "skipped", "reason": "already_complete"}
        if video.preview_status == "READY":
            return {"status": "skipped", "reason": "preview_already_ready"}

        channel = db.get(Channel, video.channel_id)
        if not channel:
            logger.error(
                "Channel %s not found for preview of video %s",
                video.channel_id,
                video_id,
            )
            return {"status": "error", "reason": "channel_not_found"}

        video.preview_status = "DOWNLOADING"
        db.commit()

        result = download_preview(
            youtube_video_id=video.youtube_video_id,
            channel_slug=channel.slug,
            video_id=video_id,
        )

        video.preview_file_path = result["file_path"]
        video.preview_status = "READY"
        db.commit()

        publish_preview_ready(video_id, user_id)

        logger.info("Preview ready: %s (%s)", video.youtube_video_id, video.title)
        return {"status": "complete", "video_id": video_id}

    except Exception as exc:
        logger.exception("Preview download failed for video %s", video_id)
        try:
            video = db.get(Video, video_id)
            if video:
                video.preview_status = None
                db.commit()
        except Exception:
            pass
        raise exc
    finally:
        db.close()
