from pydantic import BaseModel, Field


class YoutubeHandleRequest(BaseModel):
    handle: str = Field(min_length=1)


class YoutubeProfile(BaseModel):
    handle: str
    channel_id: str
    name: str
    description: str = ""
    avatar_url: str | None = None
    banner_url: str | None = None
    follower_count: int | None = None


class ChannelSuggestion(BaseModel):
    youtube_channel_id: str
    name: str
    handle: str | None = None
    avatar_url: str | None = None
    source: str
    score: int


class SuggestionsResponse(BaseModel):
    suggestions: list[ChannelSuggestion]
