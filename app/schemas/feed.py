from pydantic import BaseModel

from app.schemas.channel import ChannelOut
from app.schemas.video import VideoOut


class FeedItem(BaseModel):
    channel: ChannelOut
    video: VideoOut


class RecommendationOut(BaseModel):
    id: str
    channel_name: str
    youtube_channel_id: str | None = None
    reason: str | None = None
    dismissed: bool = False

    model_config = {"from_attributes": True}
