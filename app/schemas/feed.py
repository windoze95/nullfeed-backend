from pydantic import BaseModel

from app.schemas.channel import ChannelOut
from app.schemas.video import VideoOut


class FeedItem(BaseModel):
    channel: ChannelOut
    video: VideoOut


class HomeFeed(BaseModel):
    """Unified home payload: the three feed sections in a single response."""

    continue_watching: list[FeedItem]
    new_episodes: list[FeedItem]
    recently_added: list[FeedItem]


class RecommendationOut(BaseModel):
    id: str
    channel_name: str
    youtube_channel_id: str | None = None
    reason: str | None = None
    dismissed: bool = False

    model_config = {"from_attributes": True}
