from datetime import datetime

from pydantic import BaseModel


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
