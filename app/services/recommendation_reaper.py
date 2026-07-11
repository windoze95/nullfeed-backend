"""Delete stale Discover recommendations so they regenerate on next view.

Recommendations are a snapshot from the last generation; as channels post new
uploads and the candidate catalogue grows, they drift. This sweep deletes each
user's live (non-dismissed) recommendations older than a staleness window so
the next Discover open rebuilds them from current subscriptions — inactive
users cost nothing, since regeneration is lazy. Dismissed rows (the
do-not-recommend list) are always kept.
"""

from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.recommendation import Recommendation
from app.utils.time import utcnow_naive


def sweep_stale_recommendations(db: Session, stale_days: int) -> int:
    """Delete non-dismissed recommendations older than ``stale_days``.

    Returns the number deleted. A non-positive ``stale_days`` is a no-op.
    """
    if stale_days <= 0:
        return 0
    cutoff = utcnow_naive() - timedelta(days=stale_days)
    result = db.execute(
        delete(Recommendation).where(
            Recommendation.dismissed == False,  # noqa: E712
            Recommendation.created_at < cutoff,
        )
    )
    db.commit()
    return result.rowcount or 0
