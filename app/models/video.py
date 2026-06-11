import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base
from app.utils.time import utcnow_naive


class Video(Base):
    __tablename__ = "videos"

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
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)

    channel = relationship("Channel", back_populates="videos")
    user_refs = relationship("UserVideoRef", back_populates="video", lazy="selectin")
