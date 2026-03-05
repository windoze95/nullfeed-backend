from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base


class UserVideoRef(Base):
    __tablename__ = "user_video_refs"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), primary_key=True
    )
    video_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("videos.id"), primary_key=True
    )
    watch_position_seconds: Mapped[int] = mapped_column(Integer, default=0)
    is_watched: Mapped[bool] = mapped_column(Boolean, default=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user = relationship("User", back_populates="video_refs")
    video = relationship("Video", back_populates="user_refs")
