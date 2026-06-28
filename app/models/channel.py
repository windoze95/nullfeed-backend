import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base
from app.utils.time import utcnow_naive


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    youtube_channel_id: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    banner_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    metadata_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    # HTTP cache validators for the channel's Atom upload feed. Sent back as
    # If-None-Match / If-Modified-Since on the next routine poll so an unchanged
    # feed returns 304 Not Modified and the poll short-circuits with no yt-dlp.
    rss_etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rss_last_modified: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Adaptive poll cadence. The beat polls only channels whose next_poll_at has
    # passed; poll_interval_minutes is the current spacing, narrowed toward the
    # floor after a poll that finds uploads and widened toward the cap after an
    # empty one. Defaults make a newly added channel due immediately and start it
    # at the responsive floor (mirrors settings.poll_interval_floor_minutes).
    next_poll_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow_naive, index=True
    )
    poll_interval_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=15
    )

    videos = relationship("Video", back_populates="channel", lazy="select")
    subscriptions = relationship(
        "UserSubscription", back_populates="channel", lazy="select"
    )
