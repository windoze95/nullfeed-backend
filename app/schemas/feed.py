from pydantic import BaseModel

from app.schemas.video import VideoOut


class ContinueWatchingItem(BaseModel):
    channel_id: str
    channel_name: str
    channel_slug: str
    channel_avatar_url: str | None = None
    video: VideoOut


class NewEpisodesItem(BaseModel):
    channel_id: str
    channel_name: str
    channel_slug: str
    channel_avatar_url: str | None = None
    channel_banner_url: str | None = None
    unwatched_count: int = 0
    latest_video: VideoOut | None = None


class RecommendationOut(BaseModel):
    id: str
    channel_name: str
    youtube_channel_id: str | None = None
    reason: str | None = None
    dismissed: bool = False

    model_config = {"from_attributes": True}
