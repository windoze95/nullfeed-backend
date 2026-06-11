from fastapi import APIRouter, HTTPException

from app.schemas.youtube import (
    ChannelSuggestion,
    SuggestionsResponse,
    YoutubeHandleRequest,
    YoutubeProfile,
)
from app.services.youtube_import import (
    YoutubeResolveError,
    YoutubeResolveTimeoutError,
    get_suggestions,
    resolve_handle,
)

# Unauthenticated: both endpoints are used from the profile picker pre-login.
router = APIRouter(prefix="/api/youtube", tags=["youtube"])


@router.post("/resolve", response_model=YoutubeProfile)
async def resolve(body: YoutubeHandleRequest) -> YoutubeProfile:
    try:
        identity = await resolve_handle(body.handle)
    except YoutubeResolveTimeoutError:
        raise HTTPException(status_code=504, detail="YouTube lookup timed out")
    except YoutubeResolveError:
        raise HTTPException(status_code=404, detail="YouTube channel not found")
    return YoutubeProfile(**identity)


@router.post("/suggestions", response_model=SuggestionsResponse)
async def suggestions(body: YoutubeHandleRequest) -> SuggestionsResponse:
    try:
        items = await get_suggestions(body.handle)
    except YoutubeResolveTimeoutError:
        raise HTTPException(status_code=504, detail="YouTube lookup timed out")
    except YoutubeResolveError:
        raise HTTPException(status_code=404, detail="YouTube channel not found")
    return SuggestionsResponse(
        suggestions=[ChannelSuggestion(**item) for item in items]
    )
