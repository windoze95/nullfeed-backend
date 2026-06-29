"""Shared yt-dlp invocation helpers + YouTube cookie management.

:func:`cookie_args` is spliced into every yt-dlp call so a configured YouTube
cookies file authenticates the request — without it, age-restricted /
members-only videos fail extraction entirely ("Sign in to confirm your age").

Cookies are managed in-app (admins paste them via the settings API, see
``app/api/settings.py``) rather than hand-placed on the filesystem. Reads are
fresh on every call, so an updated cookies file takes effect immediately with no
restart. :func:`save_cookies` normalizes the paste (yt-dlp aborts on a file
whose first line isn't the Netscape header — "does not look like a Netscape
format cookies file" — which breaks *every* video, not just age-gated ones).

When extraction fails in a way that points at the cookies (bad/expired/wrong
format, age gate, bot check), :func:`note_extraction_error` records it so the
settings UI can show what's wrong instead of silently failing.
"""

import os
from datetime import datetime, timezone

from app.config import settings

_NETSCAPE_HEADERS = ("# Netscape HTTP Cookie File", "# HTTP Cookie File")

# Substrings in a yt-dlp error that mean "the cookies aren't working" — surfaced
# to the admin so they know to refresh/fix them rather than guessing.
_COOKIE_TROUBLE_MARKERS = (
    "does not look like a netscape format",  # malformed paste
    "confirm your age",  # cookies missing / not age-verified
    "inappropriate for some users",
    "confirm you're not a bot",  # session rejected / bot-flagged
    "sign in to confirm",
)

_ERROR_FILE_NAME = ".youtube_cookies_error"


def _cookies_target() -> str:
    """The path cookies are read from / written to."""
    return settings.youtube_cookies_file or os.path.join(
        settings.config_path, "cookies.txt"
    )


def _error_file() -> str:
    return os.path.join(settings.config_path, _ERROR_FILE_NAME)


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


def normalize_cookies(content: str) -> str:
    """Coerce a pasted cookies blob into a yt-dlp-loadable Netscape file.

    Strips a BOM/whitespace and, crucially, prepends the Netscape header when
    it's missing — pasting just the cookie rows (without that first line) is the
    most common way the file ends up rejected and breaking all playback.
    """
    content = content.lstrip("﻿").strip()
    first_line = content.splitlines()[0].strip() if content else ""
    if not any(first_line.startswith(h) for h in _NETSCAPE_HEADERS):
        content = f"{_NETSCAPE_HEADERS[0]}\n{content}"
    return content + "\n"


def has_cookie_rows(content: str) -> bool:
    """True if the blob contains at least one Netscape cookie row.

    A Netscape cookie line has 7 tab-separated fields (so ≥6 tabs); comment and
    blank lines don't count. Lets the API reject an empty / wrong paste clearly.
    """
    for line in content.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        if line.count("\t") >= 6:
            return True
    return False


def save_cookies(content: str) -> None:
    """Normalize and atomically write the cookies file; clear the error flag."""
    target = _cookies_target()
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    tmp = f"{target}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(normalize_cookies(content))
    os.replace(tmp, target)
    _clear_error()


def clear_cookies() -> None:
    """Remove the cookies file (and the error flag)."""
    for path in (_cookies_target(), _error_file()):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _clear_error() -> None:
    try:
        os.remove(_error_file())
    except FileNotFoundError:
        pass


def note_extraction_error(message: str) -> None:
    """Record a cookie-related extraction failure so the UI can surface it.

    No-op unless cookies are configured and the error looks cookie-related
    (works across the API + Celery processes via a file in the config volume).
    """
    if cookies_path() is None:
        return
    lowered = message.lower()
    if not any(marker in lowered for marker in _COOKIE_TROUBLE_MARKERS):
        return
    # Keep the most relevant single line, capped.
    line = next(
        (ln.strip() for ln in message.splitlines() if "error" in ln.lower()),
        message.strip().splitlines()[-1] if message.strip() else message,
    )
    try:
        with open(_error_file(), "w", encoding="utf-8") as f:
            f.write(line[:300])
    except OSError:
        pass


def cookies_status() -> dict:
    """Report cookie state for the settings UI.

    ``configured``: a cookies file is present.
    ``stale``: a cookie-related extraction error has happened since they were
    last set — they likely need refreshing/fixing.
    ``last_error``: the yt-dlp error that flagged them (so the admin can see why).
    ``updated_at``: when the cookies file was last written (ISO 8601, UTC).
    """
    path = cookies_path()
    updated_at = None
    if path is not None:
        updated_at = datetime.fromtimestamp(
            os.path.getmtime(path), tz=timezone.utc
        ).isoformat()
    last_error = None
    if path is not None and os.path.isfile(_error_file()):
        try:
            with open(_error_file(), encoding="utf-8") as f:
                last_error = f.read().strip() or None
        except OSError:
            last_error = None
    return {
        "configured": path is not None,
        "stale": last_error is not None,
        "last_error": last_error,
        "updated_at": updated_at,
    }
