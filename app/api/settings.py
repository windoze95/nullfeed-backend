"""Admin settings: in-app YouTube cookie management.

Lets an admin paste a cookies.txt from the app so age-restricted / members-only
videos can be extracted, without hand-placing a file on the server. Stored via
``app/utils/ytdlp`` (hot-reloaded on the next yt-dlp call).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.auth import get_current_user
from app.models.user import User
from app.utils.ytdlp import clear_cookies, cookies_status, save_cookies

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])


def _require_admin(user: User) -> None:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")


class YoutubeCookiesIn(BaseModel):
    cookies: str = Field(..., min_length=1, max_length=1_000_000)


@router.get("/youtube-cookies")
async def get_youtube_cookies(user: User = Depends(get_current_user)) -> dict:
    """Whether cookies are set, when, and whether they look expired."""
    _require_admin(user)
    return cookies_status()


@router.put("/youtube-cookies")
async def put_youtube_cookies(
    body: YoutubeCookiesIn, user: User = Depends(get_current_user)
) -> dict:
    """Store a pasted cookies.txt (Netscape format)."""
    _require_admin(user)
    content = body.cookies.strip()
    # Light sanity check so an obviously-wrong paste fails clearly rather than
    # silently breaking every extraction. A cookies.txt is tab-separated and/or
    # carries the standard Netscape header.
    if "\t" not in content and "# Netscape" not in content and "# HTTP" not in content:
        raise HTTPException(
            status_code=400,
            detail=(
                "That doesn't look like a cookies.txt — export it in Netscape "
                "format (e.g. the 'Get cookies.txt LOCALLY' browser extension)."
            ),
        )
    save_cookies(content + "\n")
    logger.info("YouTube cookies updated by admin %s", user.id)
    return cookies_status()


@router.delete("/youtube-cookies")
async def delete_youtube_cookies(user: User = Depends(get_current_user)) -> dict:
    """Remove the stored cookies."""
    _require_admin(user)
    clear_cookies()
    return cookies_status()
