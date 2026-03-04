from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base


class UserSubscription(Base):
    __tablename__ = "user_subscriptions"

    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(36), ForeignKey("channels.id"), primary_key=True)
    subscribed_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    retention_policy: Mapped[str] = mapped_column(String(20), default="KEEP_ALL")
    retention_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tracking_mode: Mapped[str] = mapped_column(String(20), default="FUTURE_ONLY")

    user = relationship("User", back_populates="subscriptions")
    channel = relationship("Channel", back_populates="subscriptions")
