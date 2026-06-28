from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base
from app.utils.time import utcnow_naive

# Ref "kind": how the user came to hold this video, which decides whether it
# counts as part of their library or is just an evictable play cache.
REF_KIND_LIBRARY = "LIBRARY"  # explicit intent: download, subscription, watch
REF_KIND_CACHE = "CACHE"  # implicit: created by playing a not-downloaded video


class UserVideoRef(Base):
    __tablename__ = "user_video_refs"
    __table_args__ = (
        Index("ix_user_video_refs_video_id_removed", "video_id", "removed_at"),
    )

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), primary_key=True
    )
    video_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("videos.id"), primary_key=True
    )
    watch_position_seconds: Mapped[int] = mapped_column(Integer, default=0)
    is_watched: Mapped[bool] = mapped_column(Boolean, default=False)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_watched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # See REF_KIND_*. LIBRARY refs are the user's collection (shown in the
    # library/Downloads, governed by per-subscription retention); CACHE refs are
    # created by playing a not-yet-downloaded video and are evicted by the cache
    # reaper (LRU), so playing never silently builds a collection.
    kind: Mapped[str] = mapped_column(
        String(20),
        default=REF_KIND_LIBRARY,
        server_default=REF_KIND_LIBRARY,
        nullable=False,
    )

    user = relationship("User", back_populates="video_refs")
    video = relationship("Video", back_populates="user_refs")
