"""Self-healing for downloads stranded by a crashed or killed worker.

A worker that dies mid-download (OOM kill, container restart, SIGKILL) leaves
its Video row stuck in DOWNLOADING or CANCELLING forever: the in-process
watchdog and Celery time limits can't fire because the process is simply gone.
The reaper detects these rows by a stale ``download_heartbeat_at`` and resets
them so they can run again (DOWNLOADING) or settle (CANCELLING).
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.video import Video
from app.utils.time import utcnow_naive

logger = logging.getLogger(__name__)

# How long a DOWNLOADING/CANCELLING row may go without a heartbeat before it is
# treated as stranded. A healthy download refreshes its heartbeat on yt-dlp
# output and only goes quiet during the brief post-download merge, so the worst
# healthy gap is ~NO_OUTPUT_TIMEOUT_SECONDS (300s). This sits well above that to
# avoid reaping a live download, while still recovering a crash within the hour.
STUCK_DOWNLOAD_THRESHOLD_SECONDS = 1800  # 30 minutes


def reap_stuck_downloads(db: Session, now: datetime | None = None) -> dict:
    """Reset rows stranded by a dead worker. Returns a summary dict.

    * DOWNLOADING -> PENDING and returned in ``requeue_ids`` so the caller can
      re-enqueue the download (the worker start guard skips DOWNLOADING, so the
      row must be PENDING again to be retried).
    * CANCELLING -> CATALOGED: the cancel can never be confirmed by the dead
      worker, so honor it and make the row re-downloadable.

    The function performs no Celery I/O so it stays unit-testable; re-enqueueing
    is left to the task wrapper.
    """
    now = now or utcnow_naive()
    cutoff = now - timedelta(seconds=STUCK_DOWNLOAD_THRESHOLD_SECONDS)

    stmt = select(Video).where(
        Video.status.in_(("DOWNLOADING", "CANCELLING")),
        or_(
            Video.download_heartbeat_at.is_(None),
            Video.download_heartbeat_at < cutoff,
        ),
    )
    stuck = db.execute(stmt).scalars().all()

    requeue_ids: list[str] = []
    reset_cancelling = 0
    for video in stuck:
        if video.status == "CANCELLING":
            video.status = "CATALOGED"
            reset_cancelling += 1
        else:  # DOWNLOADING
            video.status = "PENDING"
            requeue_ids.append(video.id)

    if stuck:
        db.commit()
        logger.warning(
            "Reaped %d stranded download(s): %d DOWNLOADING->PENDING, "
            "%d CANCELLING->CATALOGED",
            len(stuck),
            len(requeue_ids),
            reset_cancelling,
        )

    return {
        "requeue_ids": requeue_ids,
        "reset_downloading": len(requeue_ids),
        "reset_cancelling": reset_cancelling,
    }
