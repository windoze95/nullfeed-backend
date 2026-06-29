"""Evict the play cache (CACHE-kind UserVideoRefs) by LRU.

Playing a not-yet-downloaded video records a CACHE ref and may back it with an
HQ download (see ``POST /api/videos/{id}/cache``). Those refs are a cache, not a
collection, so this periodic sweep bounds how many a user accumulates: it keeps
the most-recently-watched ``cache_retention_count`` per user and soft-removes the
rest, then reuses the shared orphan cleanup
(:func:`app.services.storage.check_and_delete_orphan_sync`) to reclaim files no
remaining active ref still wants.

Two things are never evicted here: LIBRARY refs (the collection — governed by
per-subscription retention instead) and any cache video the user has put in their
watch-later queue ("want to watch later" is intent to keep, so queued videos are
pinned and don't count toward the budget).

Like the other reapers it does no Celery I/O so it stays unit-testable; the
scheduling lives in the task wrapper.
"""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.config import settings
from app.models.subscription import UserSubscription
from app.models.user_queue import UserQueue
from app.models.user_video_ref import REF_KIND_CACHE, UserVideoRef
from app.models.video import Video
from app.services.storage import check_and_delete_orphan_sync
from app.utils.time import utcnow_naive

logger = logging.getLogger(__name__)


def _cache_refs_to_drop(db: DBSession, user_id: str, keep: int) -> list[UserVideoRef]:
    """Return the user's evictable CACHE refs to soft-remove: those beyond the
    newest ``keep``, after pinning videos that must not be dropped here.

    Pinned (never evicted, and excluded from the budget): videos in the user's
    watch-later queue, and episodes of channels the user follows (those are a
    deliberate background cache bounded per-channel by subscription retention).
    So this LRU budget only applies to incidental cold-press cache.

    A negative ``keep`` disables eviction (returns nothing); ``keep == 0`` drops
    every non-pinned cache ref.
    """
    if keep < 0:
        return []

    rows = db.execute(
        select(UserVideoRef, Video.channel_id)
        .join(Video, UserVideoRef.video_id == Video.id)
        .where(
            UserVideoRef.user_id == user_id,
            UserVideoRef.removed_at.is_(None),
            UserVideoRef.kind == REF_KIND_CACHE,
        )
        # Most-recently-watched first; never-watched cache (last_watched_at
        # NULL) sorts last, with added_at as a stable tiebreaker.
        .order_by(
            UserVideoRef.last_watched_at.desc().nullslast(),
            UserVideoRef.added_at.desc(),
        )
    ).all()

    queued = set(
        db.execute(select(UserQueue.video_id).where(UserQueue.user_id == user_id))
        .scalars()
        .all()
    )
    followed = set(
        db.execute(
            select(UserSubscription.channel_id).where(
                UserSubscription.user_id == user_id
            )
        )
        .scalars()
        .all()
    )

    evictable = [
        ref
        for ref, channel_id in rows
        if ref.video_id not in queued and channel_id not in followed
    ]
    return evictable[keep:]


def enforce_cache_retention(db: DBSession, now: datetime | None = None) -> dict:
    """Evict stale play-cache refs across all users. Returns a summary dict.

    Soft-removes each user's CACHE refs beyond the newest
    ``settings.cache_retention_count`` (committed in one batch), then runs the
    orphan cleanup on the affected videos so files no other active ref still
    wants are reclaimed and their rows reset to ``CATALOGED``.
    """
    now = now or utcnow_naive()
    keep = settings.cache_retention_count

    user_ids = (
        db.execute(
            select(UserVideoRef.user_id)
            .where(
                UserVideoRef.removed_at.is_(None),
                UserVideoRef.kind == REF_KIND_CACHE,
            )
            .distinct()
        )
        .scalars()
        .all()
    )

    affected_video_ids: set[str] = set()
    refs_removed = 0
    for user_id in user_ids:
        for ref in _cache_refs_to_drop(db, user_id, keep):
            ref.removed_at = now
            affected_video_ids.add(ref.video_id)
            refs_removed += 1

    if refs_removed:
        db.commit()

    reclaimed = 0
    for video_id in affected_video_ids:
        if check_and_delete_orphan_sync(video_id, db):
            reclaimed += 1

    if refs_removed:
        logger.info(
            "Cache sweep: %d user(s) with cache, %d ref(s) soft-removed, "
            "%d file(s) reclaimed",
            len(user_ids),
            refs_removed,
            reclaimed,
        )

    return {
        "users": len(user_ids),
        "refs_removed": refs_removed,
        "reclaimed": reclaimed,
    }
