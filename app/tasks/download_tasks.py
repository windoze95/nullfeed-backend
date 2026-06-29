import logging
import os
import time
from datetime import datetime

from celery import group
from celery.signals import worker_ready
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.database import register_sqlite_pragmas
from app.tasks.celery_app import celery_app
from app.models.channel import Channel
from app.models.subscription import UserSubscription
from app.models.video import Video
from app.services.channel_poller import (
    _backoff_failed_channel,
    ingest_pushed_videos,
    list_channel_ids_to_poll,
    poll_single_channel,
    refresh_stale_channel_metadata,
)
from app.services.download_manager import (
    OVERALL_DEADLINE_SECONDS,
    DownloadCancelled,
    download_preview,
    download_video,
)
from app.services.ad_segments import resolve_ad_segments
from app.services.cache_retention import enforce_cache_retention
from app.services.download_reaper import reap_stuck_downloads
from app.services.retention import enforce_retention
from app.services.session_reaper import reap_expired_sessions
from app.services.websub import sync_subscriptions
from app.utils.time import utcnow_naive
from app.services.progress_broadcaster import (
    publish_download_complete,
    publish_download_progress,
    publish_preview_ready,
)

logger = logging.getLogger(__name__)

# Throttle how often the worker writes a download heartbeat to the DB. yt-dlp
# emits output far more often than this; we only need a periodic liveness mark
# for the reaper, not a write per line.
HEARTBEAT_WRITE_INTERVAL_SECONDS = 30.0

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
def poll_all_channels_task(self, due_only: bool = False) -> dict:
    """Fan the channel poll out into one isolated per-channel job each.

    The beat passes ``due_only=True`` so each frequent run only enumerates the
    channels whose adaptive schedule has come due; the pull-to-refresh "poll
    all" endpoint calls it with the default ``due_only=False`` to refresh every
    subscribed channel now. Either way this task does only the cheap, indexed
    due-check here and dispatches a Celery GROUP of ``poll_channel_task`` jobs,
    one per channel. Each job runs on its OWN DB session, so a single slow or
    stuck channel (yt-dlp/RSS) no longer blocks or aborts the others, and each
    job reschedules and enqueues auto-downloads for its own channel.
    """
    db = _get_sync_db()
    try:
        channel_ids = list_channel_ids_to_poll(db, due_only=due_only)
    except Exception:
        logger.exception("Error enumerating channels to poll")
        return {"status": "error"}
    finally:
        db.close()

    if not channel_ids:
        return {"status": "ok", "dispatched": 0}

    group(poll_channel_task.s(channel_id) for channel_id in channel_ids).apply_async()
    logger.info(
        "Dispatched %d per-channel poll jobs (due_only=%s)", len(channel_ids), due_only
    )
    return {"status": "ok", "dispatched": len(channel_ids)}


@celery_app.task(
    name="app.tasks.download_tasks.poll_channel_task",
    bind=True,
    max_retries=0,
)
def poll_channel_task(self, channel_id: str) -> dict:
    """Poll a single channel and enqueue downloads for auto-download candidates.

    The unit of work the periodic fan-out dispatches (also the immediate poll on
    subscribe). Runs on its own DB session and absorbs its own failures so one
    channel can't affect another: on error it rolls back and widens the
    channel's adaptive cadence — exactly what the old sequential loop did — so a
    persistently broken channel backs off instead of being re-dispatched every
    beat.
    """
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
        db.rollback()
        _backoff_failed_channel(channel_id, db)
        return {"status": "error"}
    finally:
        db.close()


@celery_app.task(
    name="app.tasks.download_tasks.ingest_websub_push_task",
    bind=True,
    max_retries=0,
)
def ingest_websub_push_task(self, channel_id: str, youtube_video_ids: list) -> dict:
    """Catalog WebSub-pushed video ids off the callback request path.

    The callback verifies the push signature and dispatches this job so the
    (possibly slow) yt-dlp metadata fetch never blocks the hub's HTTP request.
    Runs on its own DB session, catalogs only genuinely-new ids (idempotent for
    the duplicate pushes the hub commonly sends), and enqueues any auto-download
    candidates exactly as a normal poll would.
    """
    db = _get_sync_db()
    try:
        result = ingest_pushed_videos(channel_id, youtube_video_ids, db)
        for video_id in result["auto_download_ids"]:
            download_video_task.delay(video_id)
        return {
            "status": "ok",
            "cataloged": len(result["cataloged_ids"]),
            "auto_downloads": len(result["auto_download_ids"]),
        }
    except Exception:
        logger.exception("Error ingesting WebSub push for channel %s", channel_id)
        db.rollback()
        return {"status": "error"}
    finally:
        db.close()


@celery_app.task(
    name="app.tasks.download_tasks.sync_websub_subscriptions_task",
    bind=True,
    max_retries=0,
)
def sync_websub_subscriptions_task(self) -> dict:
    """Periodic task: (re)subscribe tracked UC channels to the WebSub hub.

    No-ops when WebSub is disabled (blank callback URL). Otherwise subscribes
    each tracked channel whose lease is missing or near expiry, renewing before
    it lapses. Independent of polling, which stays the always-on fallback.
    """
    db = _get_sync_db()
    try:
        return sync_subscriptions(db)
    except Exception:
        logger.exception("Error syncing WebSub subscriptions")
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
    # Backstop for a task the in-process watchdog somehow can't unstick (e.g. a
    # hang in our own code rather than yt-dlp). Sits just above the download
    # watchdog's overall deadline so the watchdog normally fires first and can
    # clean up; the soft limit raises a catchable error, the hard limit kills.
    soft_time_limit=OVERALL_DEADLINE_SECONDS + 300,
    time_limit=OVERALL_DEADLINE_SECONDS + 420,
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

        # Transition to DOWNLOADING and stamp the initial heartbeat so the
        # reaper has a fresh liveness mark from the moment the row goes active.
        video.status = "DOWNLOADING"
        video.download_heartbeat_at = utcnow_naive()
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

        last_heartbeat = [time.monotonic()]

        def _heartbeat() -> None:
            """Refresh the DB heartbeat so a crashed worker becomes detectable.

            Uses its own short-lived session to avoid disturbing the task's main
            transaction, and is throttled so a chatty download isn't a write
            storm.
            """
            now_m = time.monotonic()
            if now_m - last_heartbeat[0] < HEARTBEAT_WRITE_INTERVAL_SECONDS:
                return
            last_heartbeat[0] = now_m
            hb_db = _get_sync_db()
            try:
                hb_db.execute(
                    update(Video)
                    .where(Video.id == video_id)
                    .values(download_heartbeat_at=utcnow_naive())
                )
                hb_db.commit()
            except Exception:
                logger.debug("Heartbeat update failed for video %s", video_id)
            finally:
                hb_db.close()

        # Perform the download
        result = download_video(
            youtube_video_id=video.youtube_video_id,
            channel_slug=channel.slug,
            quality=quality or settings.media_quality,
            progress_callback=progress_cb,
            cancel_check=_is_cancelled,
            heartbeat_callback=_heartbeat,
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


@celery_app.task(
    name="app.tasks.download_tasks.detect_ad_segments_task",
    bind=True,
    max_retries=0,
)
def detect_ad_segments_task(self, video_id: str) -> dict:
    """Detect sponsor/ad segments (SponsorBlock + AI fallback) and store them for
    client-side skipping. ad_segments_status -> READY (list may be empty); reset
    to NULL on failure so a later request can retry."""
    db = _get_sync_db()
    try:
        video = db.get(Video, video_id)
        if not video:
            return {"status": "error", "reason": "not_found"}
        segments = resolve_ad_segments(video.youtube_video_id)
        video.ad_segments = segments
        video.ad_segments_status = "READY"
        db.commit()
        return {"status": "ok", "count": len(segments)}
    except Exception:
        logger.exception("Ad-segment detection failed for video %s", video_id)
        try:
            video = db.get(Video, video_id)
            if video:
                video.ad_segments_status = None  # allow a retry
                db.commit()
        except Exception:
            pass
        return {"status": "error"}
    finally:
        db.close()


@celery_app.task(
    name="app.tasks.download_tasks.reap_stuck_downloads_task",
    bind=True,
    max_retries=0,
)
def reap_stuck_downloads_task(self) -> dict:
    """Recover downloads stranded by a crashed/killed worker.

    Runs periodically (Celery beat) and once on worker startup. Resets stale
    DOWNLOADING rows to PENDING and re-enqueues them, and settles stale
    CANCELLING rows to CATALOGED.
    """
    db = _get_sync_db()
    try:
        result = reap_stuck_downloads(db)
    except Exception:
        logger.exception("Error in reap_stuck_downloads_task")
        return {"status": "error"}
    finally:
        db.close()

    for video_id in result["requeue_ids"]:
        download_video_task.delay(video_id)

    return {"status": "ok", **result}


@celery_app.task(
    name="app.tasks.download_tasks.enforce_retention_task",
    bind=True,
    max_retries=0,
)
def enforce_retention_task(self) -> dict:
    """Periodic task: apply each subscription's retention policy.

    Soft-removes the user's refs to downloaded videos the policy no longer keeps
    (e.g. beyond the newest N for KEEP_LAST_N), then reuses the orphan cleanup to
    reclaim any file no remaining active ref still wants.
    """
    db = _get_sync_db()
    try:
        result = enforce_retention(db)
        return {"status": "ok", **result}
    except Exception:
        logger.exception("Error in enforce_retention_task")
        return {"status": "error"}
    finally:
        db.close()


@celery_app.task(
    name="app.tasks.download_tasks.enforce_video_cache_task",
    bind=True,
    max_retries=0,
)
def enforce_video_cache_task(self) -> dict:
    """Periodic task: evict stale play-cache refs (LRU) per user.

    Soft-removes each user's CACHE refs beyond the most-recently-watched budget
    (watch-later-queued videos pinned), then reuses the orphan cleanup to reclaim
    any file no remaining active ref still wants.
    """
    db = _get_sync_db()
    try:
        result = enforce_cache_retention(db)
        return {"status": "ok", **result}
    except Exception:
        logger.exception("Error in enforce_video_cache_task")
        return {"status": "error"}
    finally:
        db.close()


@celery_app.task(
    name="app.tasks.download_tasks.reap_expired_sessions_task",
    bind=True,
    max_retries=0,
)
def reap_expired_sessions_task(self) -> dict:
    """Periodic task: delete auth sessions past their absolute/idle lifetime."""
    db = _get_sync_db()
    try:
        deleted = reap_expired_sessions(db)
        return {"status": "ok", "deleted": deleted}
    except Exception:
        logger.exception("Error in reap_expired_sessions_task")
        return {"status": "error"}
    finally:
        db.close()


@worker_ready.connect
def _reap_on_worker_startup(**_kwargs: object) -> None:
    """On worker boot, recover anything a previous (crashed) worker stranded."""
    try:
        reap_stuck_downloads_task.delay()
    except Exception:
        logger.exception("Failed to schedule startup download reap")
