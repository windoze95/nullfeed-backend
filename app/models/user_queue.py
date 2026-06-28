from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base
from app.utils.time import utcnow_naive


class UserQueue(Base):
    """A per-user watch-later queue entry.

    One row per (user, video) the user has queued. Unlike ``UserVideoRef`` this
    is a pure bookmark: it carries no download ref-count semantics, so queuing a
    video never starts or keeps alive a download. The queue is ordered oldest
    first by ``added_at`` (with ``video_id`` as a stable tiebreaker) so new
    entries append at the end. Re-adding a video is a no-op that preserves its
    original position.
    """

    __tablename__ = "user_queue"
    __table_args__ = (Index("ix_user_queue_user_added", "user_id", "added_at"),)

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), primary_key=True
    )
    video_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("videos.id"), primary_key=True
    )
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
