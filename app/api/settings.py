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
from app.utils.ytdlp import (
    clear_cookies,
    cookies_status,
    has_cookie_rows,
    normalize_cookies,
    note_extraction_error,
    save_cookies,
    verify_cookies,
)

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
    # Validate there are real cookie rows (yt-dlp aborts on a file that isn't a
    # valid Netscape cookies file, which breaks every video). The missing header
    # — the most common mistake — is repaired by save_cookies/normalize_cookies.
    if not has_cookie_rows(normalize_cookies(body.cookies)):
        raise HTTPException(
            status_code=400,
            detail=(
                "That doesn't look like a cookies.txt — it has no cookie entries. "
                "Export it in Netscape format (the 'Get cookies.txt LOCALLY' "
                "extension's Export does this) and paste the whole file."
            ),
        )
    save_cookies(body.cookies)
    # Verify they actually work so the UI reports "connected" only when true.
    error = verify_cookies()
    if error:
        note_extraction_error(error)
    logger.info(
        "YouTube cookies updated by admin %s (working=%s)", user.id, error is None
    )
    return cookies_status()


@router.delete("/youtube-cookies")
async def delete_youtube_cookies(user: User = Depends(get_current_user)) -> dict:
    """Remove the stored cookies."""
    _require_admin(user)
    clear_cookies()
    return cookies_status()
