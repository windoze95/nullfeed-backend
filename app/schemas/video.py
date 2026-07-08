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
    # Why YouTube refuses this video (age_restricted, members_only, premium,
    # private, geo_blocked, removed, drm, upcoming, unavailable), or None when
    # playable as far as we know. Clients render this as a banner.
    unplayable_reason: str | None = None
    # What kind of media this is (regular, short, live, premiere, age_restricted,
    # members_only, premium), or None for rows cataloged before the field
    # existed. Clients badge it and gate it per channel.
    content_type: str | None = None
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


class PrewarmRequest(BaseModel):
    """Ids of videos the client expects the user to play soon, so the backend can
    pre-generate their previews. Bounded server-side; extra ids are ignored."""

    video_ids: list[str] = Field(default_factory=list)


class VideoPagination(BaseModel):
    items: list[VideoOut]
    total: int
    page: int
    per_page: int


class VideoSearchPage(BaseModel):
    """Cursor-paginated search results.

    ``next_cursor`` is an opaque token for the next page, or ``None`` on the last
    page. ``total`` is the full count of matching rows, independent of the cursor.
    """

    items: list[VideoOut]
    total: int
    next_cursor: str | None = None
