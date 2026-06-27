from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base
from app.utils.time import utcnow_naive


class UserSubscription(Base):
    __tablename__ = "user_subscriptions"
    __table_args__ = (Index("ix_user_subscriptions_channel_id", "channel_id"),)

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), primary_key=True
    )
    channel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("channels.id"), primary_key=True
    )
    subscribed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    retention_policy: Mapped[str] = mapped_column(String(20), default="KEEP_ALL")
    retention_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tracking_mode: Mapped[str] = mapped_column(String(20), default="FUTURE_ONLY")

    user = relationship("User", back_populates="subscriptions")
    channel = relationship("Channel", back_populates="subscriptions")
