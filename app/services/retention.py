"""Apply each subscription's retention policy.

The ``retention_policy`` / ``retention_count`` fields on a ``UserSubscription``
let a self-hoster bound how many downloaded videos a channel keeps on disk. This
module is the enforcement the periodic Celery task drives: for every
subscription that sets a policy, it soft-removes the user's ``UserVideoRef`` for
the downloaded videos the policy no longer keeps, then reuses the orphan cleanup
(:func:`app.services.storage.check_and_delete_orphan_sync`) to reclaim any file
no remaining active ref still wants. Doing it this way keeps the ref-count the
single source of truth for shared downloads: soft-removing one user's ref never
deletes a file another user is still holding.

Retention is scoped per subscription (per user, per channel) because that is
where the fields live, and it only ever considers videos the user actually
downloaded (``status == "COMPLETE"``) — cataloged-but-not-downloaded videos
occupy no disk and are left untouched.

Like the other reapers this does no Celery I/O so it stays unit-testable; the
scheduling lives in the task wrapper.
"""

import logging
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.models.subscription import UserSubscription
from app.models.user_video_ref import REF_KIND_CACHE, UserVideoRef
from app.models.video import Video
from app.services.storage import check_and_delete_orphan_sync
from app.utils.time import utcnow_naive

logger = logging.getLogger(__name__)

# Known retention policies (the wire values the clients send).
KEEP_ALL = "KEEP_ALL"  # default — keep everything, nothing to enforce
KEEP_LAST_N = "KEEP_LAST_N"  # keep the newest N downloaded videos
# KEEP_WATCHED is a client-defined value but is intentionally NOT enforced here:
# its only sensible reading ("delete videos you have not watched yet") is
# destructive, so we treat it as a no-op until the product semantics are pinned
# down rather than risk removing videos a user still intends to watch.
KEEP_WATCHED = "KEEP_WATCHED"


def _refs_to_drop_for_subscription(
    db: DBSession, sub: UserSubscription
) -> list[UserVideoRef]:
    """Return the active refs this subscription's policy says to soft-remove.

    Only the user's *downloaded* (``COMPLETE``) videos in the channel are
    considered. Returns an empty list for policies that keep everything, are
    unconfigured, or are not enforced.
    """
    if sub.retention_policy == KEEP_LAST_N:
        keep = sub.retention_count
        # Without a positive count there is nothing to enforce; a non-positive
        # count is treated as a misconfiguration rather than "delete all".
        if keep is None or keep < 1:
            return []
        # The user's downloaded videos in this channel, newest first. Sort key
        # mirrors how the app presents videos (upload date, falling back to
        # catalog time) with a stable id tiebreak so "newest N" is deterministic.
        refs = (
            db.execute(
                select(UserVideoRef)
                .join(Video, UserVideoRef.video_id == Video.id)
                .where(
                    Video.channel_id == sub.channel_id,
                    Video.status == "COMPLETE",
                    UserVideoRef.user_id == sub.user_id,
                    UserVideoRef.removed_at.is_(None),
                    # Followed-channel episodes are cached (CACHE refs). This is
                    # where their disk use is bounded per channel; the global
                    # cache reaper deliberately leaves followed-channel videos to
                    # this policy and only evicts cold-press cache.
                    UserVideoRef.kind == REF_KIND_CACHE,
                )
                .order_by(
                    func.coalesce(Video.uploaded_at, Video.created_at).desc(),
                    Video.id.desc(),
                )
            )
            .scalars()
            .all()
        )
        # Keep the newest `keep`; everything older is dropped.
        return list(refs[keep:])

    # KEEP_ALL, KEEP_WATCHED, or an unknown value: nothing to drop.
    return []


def enforce_retention(db: DBSession, now: datetime | None = None) -> dict:
    """Apply every subscription's retention policy. Returns a summary dict.

    Soft-removes the refs each policy no longer keeps (committed in one batch),
    then runs the orphan cleanup on the affected videos so files no other active
    ref still wants are reclaimed and their rows reset to ``CATALOGED``.
    """
    now = now or utcnow_naive()

    # Only subscriptions that actually set a policy can do work.
    subs = (
        db.execute(
            select(UserSubscription).where(
                UserSubscription.retention_policy != KEEP_ALL
            )
        )
        .scalars()
        .all()
    )

    affected_video_ids: set[str] = set()
    refs_removed = 0
    for sub in subs:
        for ref in _refs_to_drop_for_subscription(db, sub):
            ref.removed_at = now
            affected_video_ids.add(ref.video_id)
            refs_removed += 1

    if refs_removed:
        db.commit()

    # Reuse the existing orphan cleanup: it deletes the file and resets the row
    # only when no active ref remains, so a video another user still holds is
    # left intact (shared downloads stay ref-counted).
    reclaimed = 0
    for video_id in affected_video_ids:
        if check_and_delete_orphan_sync(video_id, db):
            reclaimed += 1

    if refs_removed:
        logger.info(
            "Retention sweep: %d subscription(s) with a policy, "
            "%d ref(s) soft-removed, %d file(s) reclaimed",
            len(subs),
            refs_removed,
            reclaimed,
        )

    return {
        "subscriptions": len(subs),
        "refs_removed": refs_removed,
        "reclaimed": reclaimed,
    }
