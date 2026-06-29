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
import subprocess
from datetime import datetime, timezone

from app.config import settings

_NETSCAPE_HEADERS = ("# Netscape HTTP Cookie File", "# HTTP Cookie File")

# Probe videos used to verify cookies on save (so "connected" means verified).
# The age-restricted one is the meaningful test — the whole point of cookies is
# age-restricted playback — with the normal one as a fallback to tell "session
# broken" from "session ok but not age-authorized".
_AGE_PROBE_VIDEO_ID = "HtVdAasjOgU"  # stable age-restricted (yt-dlp test video)
_PROBE_VIDEO_ID = "dQw4w9WgXcQ"  # stable, public, non-restricted

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

    Two common copy/paste breakages are repaired, because yt-dlp aborts on
    either and that breaks every video:
    * tabs turned into spaces — a Netscape cookie row is 7 fields whose first
      six never contain spaces, so space-separated rows are rejoined with tabs;
    * a missing ``# Netscape HTTP Cookie File`` header — prepended when absent.
    """
    fixed: list[str] = []
    for line in content.lstrip("﻿").strip().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            fixed.append(line)
            continue
        if "\t" not in line:
            parts = stripped.split()
            # Only rewrite lines shaped like a Netscape cookie row (flag fields
            # are TRUE/FALSE and the expiry is numeric) — never plain prose.
            if (
                len(parts) >= 7
                and parts[1] in ("TRUE", "FALSE")
                and parts[3] in ("TRUE", "FALSE")
                and parts[4].lstrip("-").isdigit()
            ):
                # Keep any spaces inside the value (the 7th field).
                fixed.append("\t".join(parts[:6]) + "\t" + " ".join(parts[6:]))
                continue
        fixed.append(line)
    out = "\n".join(fixed)
    first_line = out.splitlines()[0].strip() if out else ""
    if not any(first_line.startswith(h) for h in _NETSCAPE_HEADERS):
        out = f"{_NETSCAPE_HEADERS[0]}\n{out}"
    return out + "\n"


def _probe_error(video_id: str) -> str | None:
    """``yt-dlp --simulate`` with the saved cookies; the last error line, or None
    on success / no cookies / a slow probe."""
    path = cookies_path()
    if path is None:
        return None
    cmd = [
        "yt-dlp",
        "--cookies",
        path,
        "--simulate",
        "--no-warnings",
        "--no-playlist",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode == 0:
        return None
    err = (result.stderr or result.stdout or "").strip()
    lines = [ln.strip() for ln in err.splitlines() if "error" in ln.lower()]
    chosen = lines[-1] if lines else (err.splitlines() or [""])[-1]
    return chosen[:300]


def verify_cookies() -> str | None:
    """Verify the saved cookies actually work, so the UI reports "connected" only
    when true. Returns a short reason string if not working, else None.

    Probes an *age-restricted* video (the whole point of cookies). If it resolves
    the cookies fully work; an age gate means the session loads but isn't
    age-authorized; a malformed-file / bot error is surfaced verbatim. A failure
    unrelated to cookies (e.g. the probe video was removed) falls back to a
    normal video so a genuinely broken session is still caught.
    """
    if cookies_path() is None:
        return "No cookies file."
    age_err = _probe_error(_AGE_PROBE_VIDEO_ID)
    if age_err is None:
        return None  # age-restricted resolved → fully working
    low = age_err.lower()
    if "confirm your age" in low or "inappropriate for some users" in low:
        return (
            "Cookies load, but don't unlock age-restricted videos — the account "
            "may not be age-verified, or the cookies have gone stale. Re-export "
            "from a browser that's signed in to YouTube."
        )
    if any(m in low for m in _COOKIE_TROUBLE_MARKERS):
        return age_err
    # Age probe failed for an unrelated reason; confirm the session at least.
    normal_err = _probe_error(_PROBE_VIDEO_ID)
    if normal_err and any(m in normal_err.lower() for m in _COOKIE_TROUBLE_MARKERS):
        return normal_err
    return None


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


def set_cookies_error(message: str) -> None:
    """Record a cookie problem so the settings UI surfaces it (cleared on save).

    Cross-process safe (a file in the config volume), so a failure seen in the
    API or a Celery worker both reach the panel.
    """
    try:
        with open(_error_file(), "w", encoding="utf-8") as f:
            f.write(message.strip()[:300])
    except OSError:
        pass


def note_extraction_error(message: str) -> None:
    """Record a cookie-related extraction failure (from the playback path) so the
    UI can surface it. No-op unless cookies are configured and the error looks
    cookie-related."""
    if cookies_path() is None:
        return
    lowered = message.lower()
    if not any(marker in lowered for marker in _COOKIE_TROUBLE_MARKERS):
        return
    line = next(
        (ln.strip() for ln in message.splitlines() if "error" in ln.lower()),
        message.strip().splitlines()[-1] if message.strip() else message,
    )
    set_cookies_error(line)


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
