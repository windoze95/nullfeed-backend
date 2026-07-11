"""Admin settings: YouTube cookies + AI provider configuration.

Lets an admin paste a cookies.txt from the app so age-restricted / members-only
videos can be extracted, without hand-placing a file on the server (stored via
``app/utils/ytdlp``, hot-reloaded on the next yt-dlp call), and manage the
Discover AI provider keys and embed/rank selection (``app/services/ai_config``).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.auth import get_current_user
from app.models.user import User
from app.services import ai_config, llm_providers
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


class AiKeyIn(BaseModel):
    key: str = Field(..., min_length=1, max_length=500)


class AiSelectionIn(BaseModel):
    provider: str = Field("", max_length=40)
    model: str = Field("", max_length=120)


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


# --- AI providers: runtime keys + provider/model selection -----------------


def _ai_status() -> dict:
    """Full AI-config view for the admin panel. Never includes a raw key."""
    embed_eff = llm_providers.resolve_embed_provider()
    rank_eff = llm_providers.resolve_rank_provider()
    return {
        "keys": {p: ai_config.key_status(p) for p in ai_config.KEY_PROVIDERS},
        "embed": {
            **ai_config.selection_status("embed"),
            "effective": {"provider": embed_eff[0], "model": embed_eff[1]}
            if embed_eff
            else None,
            "options": list(llm_providers.EMBED_MODELS),
        },
        "rank": {
            **ai_config.selection_status("rank"),
            "effective": {"provider": rank_eff[0], "model": rank_eff[1]}
            if rank_eff
            else None,
            "options": list(llm_providers.RANK_MODELS),
        },
        "availability": {
            "anthropic": bool(ai_config.get_key("anthropic")),
            "gemini": bool(ai_config.get_key("gemini")),
            "openai": bool(ai_config.get_key("openai")),
        },
    }


@router.get("/ai-providers")
async def get_ai_providers(user: User = Depends(get_current_user)) -> dict:
    """Which keys are set (masked), the embed/rank selection, and what's live."""
    _require_admin(user)
    return _ai_status()


@router.put("/ai-providers/keys/{provider}")
async def put_ai_key(
    provider: str, body: AiKeyIn, user: User = Depends(get_current_user)
) -> dict:
    """Set a provider API key at runtime (overrides the env value)."""
    _require_admin(user)
    if provider not in ai_config.KEY_PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    ai_config.set_key(provider, body.key.strip())
    # Name + admin id only — never the key value.
    logger.info("AI %s key set by admin %s", provider, user.id)
    return _ai_status()


@router.delete("/ai-providers/keys/{provider}")
async def delete_ai_key(provider: str, user: User = Depends(get_current_user)) -> dict:
    """Clear a runtime key, reverting that provider to its env value (if any)."""
    _require_admin(user)
    if provider not in ai_config.KEY_PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    ai_config.clear_key(provider)
    logger.info("AI %s key cleared by admin %s", provider, user.id)
    return _ai_status()


@router.put("/ai-providers/selection/{role}")
async def put_ai_selection(
    role: str, body: AiSelectionIn, user: User = Depends(get_current_user)
) -> dict:
    """Pin the embed/rank provider (+ optional model), or "" to auto-detect."""
    _require_admin(user)
    if role not in ai_config.SELECTION_ROLES:
        raise HTTPException(status_code=404, detail="Unknown selection role")
    provider = body.provider.strip().lower()
    model = body.model.strip()
    allowed = (
        llm_providers.EMBED_MODELS if role == "embed" else llm_providers.RANK_MODELS
    )
    if provider and provider not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Provider {provider!r} is not valid for {role} "
            f"(choose from {', '.join(allowed)} or leave blank to auto-detect)",
        )
    if model and not provider:
        raise HTTPException(
            status_code=400,
            detail="Set the provider explicitly to use a model override",
        )
    ai_config.set_selection(role, provider, model)
    logger.info(
        "Discovery %s provider set to %r by admin %s", role, provider or "auto", user.id
    )
    return _ai_status()
