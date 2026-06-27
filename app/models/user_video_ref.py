from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base
from app.utils.time import utcnow_naive


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

    user = relationship("User", back_populates="video_refs")
    video = relationship("Video", back_populates="user_refs")
