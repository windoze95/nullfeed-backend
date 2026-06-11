from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class VideoOut(BaseModel):
    id: str
    youtube_video_id: str
    channel_id: str
    title: str
    duration_seconds: int = 0
    uploaded_at: datetime | None = None
    file_size_bytes: int = 0
    status: str = "CATALOGED"
    preview_status: str | None = None
    thumbnail_url: str | None = None
    watch_position_seconds: int = 0
    is_watched: bool = False
    last_watched_at: datetime | None = None
    channel_name: str = ""

    model_config = {"from_attributes": True}


class VideoDetail(VideoOut):
    metadata_json: dict | None = None
    channel_name: str = ""
    channel_slug: str = ""


class VideoProgress(BaseModel):
    position_seconds: int = Field(ge=0)
    is_watched: bool = False


class DownloadRequest(BaseModel):
    quality: Literal["720p", "1080p", "4k", "best"] | None = None


class VideoPagination(BaseModel):
    items: list[VideoOut]
    total: int
    page: int
    per_page: int
