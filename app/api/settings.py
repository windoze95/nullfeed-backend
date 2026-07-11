"""Admin settings: YouTube cookies + ChatGPT (Codex OAuth) sign-in.

Lets an admin paste a cookies.txt from the app so age-restricted / members-only
videos can be extracted, without hand-placing a file on the server (stored via
``app/utils/ytdlp``, hot-reloaded on the next yt-dlp call), and manage the
optional ChatGPT-subscription sign-in that backs the ``chatgpt`` Discover
rank provider (``app/services/chatgpt_auth``).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.auth import get_current_user
from app.models.user import User
from app.services import chatgpt_auth
from app.utils.ytdlp import (
    clear_cookies,
    cookies_status,
    has_cookie_rows,
    normalize_cookies,
    save_cookies,
    set_cookies_error,
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
    # Verify they actually work (against an age-restricted video) so the UI
    # reports "connected" only when age-restricted playback will succeed.
    error = verify_cookies()
    if error:
        set_cookies_error(error)
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


# --- ChatGPT (Codex OAuth) sign-in for the Discover rank provider ----------


@router.get("/chatgpt-login")
async def get_chatgpt_login(user: User = Depends(get_current_user)) -> dict:
    """Sign-in status: connected / pending / needs re-auth."""
    _require_admin(user)
    return chatgpt_auth.auth_status()


@router.post("/chatgpt-login")
async def start_chatgpt_login(user: User = Depends(get_current_user)) -> dict:
    """Start the device sign-in: open the returned URL, enter the code."""
    _require_admin(user)
    try:
        started = await chatgpt_auth.start_device_login()
    except chatgpt_auth.DeviceLoginError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:  # network failure etc. — never a 500 traceback
        logger.warning("ChatGPT sign-in start failed: %s", exc)
        raise HTTPException(
            status_code=502, detail="Could not reach the ChatGPT sign-in service."
        )
    logger.info("ChatGPT device sign-in started by admin %s", user.id)
    return started


@router.post("/chatgpt-login/poll")
async def poll_chatgpt_login(user: User = Depends(get_current_user)) -> dict:
    """One approval poll; repeat until status is no longer 'pending'."""
    _require_admin(user)
    try:
        return await chatgpt_auth.poll_device_login()
    except chatgpt_auth.DeviceLoginError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.warning("ChatGPT sign-in poll failed: %s", exc)
        raise HTTPException(
            status_code=502, detail="Could not reach the ChatGPT sign-in service."
        )


@router.delete("/chatgpt-login")
async def delete_chatgpt_login(user: User = Depends(get_current_user)) -> dict:
    """Sign out (removes the stored tokens)."""
    _require_admin(user)
    chatgpt_auth.clear_auth()
    logger.info("ChatGPT sign-in cleared by admin %s", user.id)
    return chatgpt_auth.auth_status()
