import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base


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

    videos = relationship("Video", back_populates="channel", lazy="select")
    subscriptions = relationship(
        "UserSubscription", back_populates="channel", lazy="select"
    )
