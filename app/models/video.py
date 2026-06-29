import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base
from app.utils.time import utcnow_naive


class Video(Base):
    __tablename__ = "videos"
    __table_args__ = (
        # /new-episodes ranks each channel's unwatched videos with a window
        # function (PARTITION BY channel_id ORDER BY uploaded_at DESC) to pick
        # the newest per channel; this composite serves that partition+order.
        Index("ix_videos_channel_id_uploaded_at", "channel_id", "uploaded_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    youtube_video_id: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False
    )
    channel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("channels.id"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    preview_file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    preview_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Detected sponsor/ad segments for client-side skipping (#88). JSON list of
    # {start, end, category} in seconds. ad_segments_status: NULL = not checked,
    # "PENDING" = detection enqueued, "READY" = checked (list may be empty when
    # no ads were found).
    ad_segments: Mapped[list | None] = mapped_column(JSON, nullable=True)
    ad_segments_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Liveness signal for an in-flight download: set when the row enters
    # DOWNLOADING and refreshed by the worker as yt-dlp produces output. The
    # reaper treats a DOWNLOADING/CANCELLING row whose heartbeat has gone stale
    # as stranded by a crashed worker and resets it. NULL means "no download
    # has touched this row since the column was added".
    download_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)

    channel = relationship("Channel", back_populates="videos")
    user_refs = relationship("UserVideoRef", back_populates="video", lazy="select")
