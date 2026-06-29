"""Shared yt-dlp invocation helpers + YouTube cookie management.

:func:`cookie_args` is spliced into every yt-dlp call so a configured YouTube
cookies file authenticates the request — without it, age-restricted /
members-only videos fail extraction entirely ("Sign in to confirm your age").

Cookies are managed in-app (admins paste them via the settings API, see
``app/api/settings.py``) rather than hand-placed on the filesystem. Reads are
fresh on every call, so an updated cookies file takes effect immediately with no
restart. When an age-gate error happens *despite* cookies being present, we flag
them as likely-expired (a sentinel file) so the app can prompt for a refresh.
"""

import os
from datetime import datetime, timezone

from app.config import settings

# Substrings that identify YouTube's age/sign-in gate in a yt-dlp error.
_AGE_GATE_MARKERS = ("confirm your age", "inappropriate for some users")

_STALE_SENTINEL_NAME = ".youtube_cookies_stale"


def _cookies_target() -> str:
    """The path cookies are read from / written to."""
    return settings.youtube_cookies_file or os.path.join(
        settings.config_path, "cookies.txt"
    )


def _stale_sentinel() -> str:
    return os.path.join(settings.config_path, _STALE_SENTINEL_NAME)


def cookies_path() -> str | None:
    """The cookies file path if it exists on disk, else None."""
    target = _cookies_target()
    return target if target and os.path.isfile(target) else None


def cookie_args() -> list[str]:
    """``["--cookies", <path>]`` when a cookies file is present, else ``[]``.

    Splice right after ``"yt-dlp"`` in any command list.
    """
    path = cookies_path()
    return ["--cookies", path] if path else []


def save_cookies(content: str) -> None:
    """Atomically write the cookies file and clear any stale flag."""
    target = _cookies_target()
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    tmp = f"{target}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, target)
    _clear_stale()


def clear_cookies() -> None:
    """Remove the cookies file (and the stale flag)."""
    for path in (_cookies_target(), _stale_sentinel()):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _clear_stale() -> None:
    try:
        os.remove(_stale_sentinel())
    except FileNotFoundError:
        pass


def note_extraction_error(message: str) -> None:
    """If an age-gate error occurred while cookies ARE configured, flag them as
    likely expired so the app can prompt for a refresh. No-op otherwise (a gate
    error with no cookies is a 'missing', not 'stale', state)."""
    if cookies_path() is None:
        return
    lowered = message.lower()
    if not any(marker in lowered for marker in _AGE_GATE_MARKERS):
        return
    try:
        with open(_stale_sentinel(), "w", encoding="utf-8") as f:
            f.write("")
    except OSError:
        pass


def cookies_status() -> dict:
    """Report cookie state for the settings UI.

    ``configured``: a cookies file is present.
    ``stale``: an age-gate error occurred since the cookies were last set —
    they've probably expired and need refreshing.
    ``updated_at``: when the cookies file was last written (ISO 8601, UTC).
    """
    path = cookies_path()
    updated_at = None
    if path is not None:
        updated_at = datetime.fromtimestamp(
            os.path.getmtime(path), tz=timezone.utc
        ).isoformat()
    return {
        "configured": path is not None,
        "stale": path is not None and os.path.isfile(_stale_sentinel()),
        "updated_at": updated_at,
    }
