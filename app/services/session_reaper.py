"""Periodic cleanup of expired auth sessions.

Sessions are rejected at resolve time once they pass their absolute or idle
lifetime (see ``app.api.auth._session_is_expired``), but the rows linger until
swept. This reaper deletes them so the table cannot grow without bound. It does
no Celery I/O, keeping it unit-testable; scheduling lives in the task wrapper.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, or_
from sqlalchemy.orm import Session as DBSession

from app.config import settings
from app.models.session import Session
from app.utils.time import utcnow_naive

logger = logging.getLogger(__name__)


def reap_expired_sessions(db: DBSession, now: datetime | None = None) -> int:
    """Delete sessions past their absolute or idle lifetime. Returns the count.

    Mirrors the expiry rule enforced on the read path: a session is expired if
    it was created before the absolute cutoff OR last seen before the idle
    cutoff. Both cutoffs come from settings so they stay in lockstep with auth.
    """
    now = now or utcnow_naive()
    absolute_cutoff = now - timedelta(days=settings.session_absolute_ttl_days)
    idle_cutoff = now - timedelta(days=settings.session_idle_ttl_days)

    result = db.execute(
        delete(Session).where(
            or_(
                Session.created_at < absolute_cutoff,
                Session.last_seen_at < idle_cutoff,
            )
        )
    )
    db.commit()

    deleted = result.rowcount or 0
    if deleted:
        logger.info("Reaped %d expired session(s)", deleted)
    return deleted
