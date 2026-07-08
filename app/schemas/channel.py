from datetime import datetime

from pydantic import BaseModel, Field


class ChannelOut(BaseModel):
    id: str
    youtube_channel_id: str
    name: str
    slug: str
    description: str = ""
    banner_url: str | None = None
    avatar_url: str | None = None
    last_checked_at: datetime | None = None
    video_count: int = 0
    is_subscribed: bool = False

    model_config = {"from_attributes": True}


class ChannelSubscribe(BaseModel):
    url: str | None = None
    youtube_channel_id: str | None = None
    retention_policy: str = "KEEP_ALL"
    retention_count: int | None = None
    tracking_mode: str = "FUTURE_ONLY"


class ChannelDetail(ChannelOut):
    subscriber_count: int = 0
    tracking_mode: str | None = None
    # Content types this user has hidden for this channel (empty when nothing is
    # hidden or the user isn't subscribed). Backs the per-channel filter menu.
    hidden_content_types: list[str] = Field(default_factory=list)


class ContentFilterUpdate(BaseModel):
    """Replace the set of content types hidden for a channel. An empty list
    clears the filter (show everything)."""

    hidden_content_types: list[str] = Field(default_factory=list)


class BulkSubscribeItem(BaseModel):
    youtube_channel_id: str = Field(min_length=1)
    name: str | None = None


class BulkSubscribeRequest(BaseModel):
    items: list[BulkSubscribeItem] = Field(min_length=1, max_length=25)


class BulkSubscribeItemResult(BaseModel):
    youtube_channel_id: str
    status: str  # "subscribed" | "already_subscribed" | "error"
    channel_id: str | None = None
    detail: str | None = None


class BulkSubscribeResponse(BaseModel):
    results: list[BulkSubscribeItemResult]
