"""YouTube channel identity resolution and channel suggestions via yt-dlp.

The contracts here (exception type and function signatures) are depended on by
app.api.auth and app.api.youtube.
"""

import asyncio
import json
import logging
import re
import subprocess
import time
from typing import Any

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 3600
RESOLVE_TIMEOUT_SECONDS = 30
SUGGESTIONS_CALL_TIMEOUT_SECONDS = 25
SUGGESTIONS_BUDGET_SECONDS = 60
MAX_PLAYLISTS = 5
MAX_PLAYLIST_ITEMS = 25
MAX_SUGGESTIONS = 15
FEATURED_SCORE = 100


class YoutubeResolveError(Exception):
    """Raised when a YouTube handle cannot be resolved to a channel."""


class YoutubeResolveTimeoutError(YoutubeResolveError):
    """Raised when a YouTube lookup exceeds its time budget."""


# In-memory TTL caches keyed by normalized handle.
_resolve_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_suggestions_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _cache_get(cache: dict[str, tuple[float, Any]], key: str) -> Any | None:
    entry = cache.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if time.monotonic() >= expires_at:
        del cache[key]
        return None
    return value


def _cache_set(cache: dict[str, tuple[float, Any]], key: str, value: Any) -> None:
    cache[key] = (time.monotonic() + CACHE_TTL_SECONDS, value)


def _normalize_handle(handle: str) -> str:
    """Normalize ``@name``, ``name``, or a full YouTube URL to ``@name``/UC id."""
    value = handle.strip()
    if "youtube.com" in value:
        for pattern in (
            r"youtube\.com/channel/(UC[A-Za-z0-9_-]+)",
            r"youtube\.com/(@[A-Za-z0-9._-]+)",
            r"youtube\.com/(?:c|user)/([A-Za-z0-9._-]+)",
        ):
            match = re.search(pattern, value)
            if match:
                value = match.group(1)
                break
        else:
            raise YoutubeResolveError(f"Could not parse YouTube URL: {handle}")
    value = value.strip().strip("/")
    if not value:
        raise YoutubeResolveError("No handle provided")
    if value.startswith("UC") and len(value) == 24:
        return value
    if not value.startswith("@"):
        value = f"@{value}"
    return value


def _channel_url(normalized: str, suffix: str = "") -> str:
    if normalized.startswith("UC"):
        return f"https://www.youtube.com/channel/{normalized}{suffix}"
    return f"https://www.youtube.com/{normalized}{suffix}"


def _run_yt_dlp_json(url: str, extra_args: list[str], timeout: int) -> dict[str, Any]:
    """Run yt-dlp with -J against a URL and return the parsed JSON document.

    Blocking; callers run this via asyncio.to_thread. Always passes
    --no-update so the version nag never pollutes stderr.
    """
    cmd = ["yt-dlp", "--no-update", "--flat-playlist", *extra_args, "-J", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise YoutubeResolveTimeoutError(f"yt-dlp timed out for {url}")
    except OSError as exc:
        logger.error("Could not run yt-dlp: %s", exc)
        raise YoutubeResolveError(f"Could not run yt-dlp: {exc}")
    if result.returncode != 0 or not result.stdout.strip():
        stderr_lines = (result.stderr or "").strip().splitlines()
        detail = stderr_lines[-1] if stderr_lines else "no output"
        raise YoutubeResolveError(f"yt-dlp failed for {url}: {detail[:300]}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise YoutubeResolveError(f"yt-dlp returned invalid JSON for {url}")
    if not isinstance(data, dict):
        raise YoutubeResolveError(f"yt-dlp returned unexpected JSON for {url}")
    return data


def _select_images(
    thumbnails: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    """Pick avatar and banner URLs from a yt-dlp thumbnails list.

    Avatar: the ``avatar_uncropped`` thumbnail, else the largest square-ish
    one. Banner: ``banner_uncropped`` if present.
    """
    avatar_uncropped: str | None = None
    banner: str | None = None
    best_square: str | None = None
    best_area = -1
    for thumb in thumbnails:
        url = thumb.get("url")
        if not url:
            continue
        thumb_id = thumb.get("id")
        if thumb_id == "avatar_uncropped":
            avatar_uncropped = url
        elif thumb_id == "banner_uncropped":
            banner = url
        width = thumb.get("width")
        height = thumb.get("height")
        if width and height and 0.8 <= width / height <= 1.25:
            area = width * height
            if area > best_area:
                best_area = area
                best_square = url
    return avatar_uncropped or best_square, banner


def _entry_handle(entry: dict[str, Any]) -> str | None:
    handle = entry.get("uploader_id")
    if isinstance(handle, str) and handle.startswith("@"):
        return handle
    return None


async def resolve_handle(handle: str) -> dict[str, Any]:
    """Resolve a YouTube handle (or channel URL) to channel identity.

    Returns a dict with keys: handle, channel_id, name, description,
    avatar_url, banner_url, follower_count.

    Raises YoutubeResolveError if the channel cannot be resolved, or
    YoutubeResolveTimeoutError (a subclass) on timeout.
    """
    normalized = _normalize_handle(handle)
    cached = _cache_get(_resolve_cache, normalized)
    if cached is not None:
        return cached

    data = await asyncio.to_thread(
        _run_yt_dlp_json,
        _channel_url(normalized),
        ["--playlist-items", "0"],
        RESOLVE_TIMEOUT_SECONDS,
    )

    channel_id = data.get("channel_id") or data.get("id")
    if not channel_id:
        raise YoutubeResolveError(f"No channel id in yt-dlp output for {normalized}")
    name = (
        data.get("channel") or data.get("uploader") or data.get("title") or normalized
    )
    resolved_handle = _entry_handle(data) or normalized

    thumbnails = data.get("thumbnails")
    avatar_url, banner_url = _select_images(
        thumbnails if isinstance(thumbnails, list) else []
    )

    follower_count = data.get("channel_follower_count")
    result: dict[str, Any] = {
        "handle": resolved_handle,
        "channel_id": channel_id,
        "name": name,
        "description": data.get("description") or "",
        "avatar_url": avatar_url,
        "banner_url": banner_url,
        "follower_count": follower_count if isinstance(follower_count, int) else None,
    }
    _cache_set(_resolve_cache, normalized, result)
    return result


async def get_suggestions(handle: str) -> list[dict[str, Any]]:
    """Return ranked suggestions of channels the given handle follows.

    Each item is a dict with keys: youtube_channel_id, name, handle,
    avatar_url, source, score. An empty list is a normal result.

    Strategy (graceful degradation, total budget ~60s):
    1. Legacy featured-channels tab (usually absent — errors are skipped).
    2. Public playlists: channel frequency across up to 5 playlists.
    """
    normalized = _normalize_handle(handle)
    cached = _cache_get(_suggestions_cache, normalized)
    if cached is not None:
        return cached

    # Resolve the user's own channel first (cached) so we can exclude it.
    identity = await resolve_handle(handle)
    own_channel_id = identity.get("channel_id")

    deadline = time.monotonic() + SUGGESTIONS_BUDGET_SECONDS
    by_channel: dict[str, dict[str, Any]] = {}

    # 1. Legacy featured channels tab. Most channels no longer have one;
    # yt-dlp errors with "does not have a channels tab" — skip and continue.
    try:
        data = await asyncio.to_thread(
            _run_yt_dlp_json,
            _channel_url(normalized, "/channels"),
            [],
            SUGGESTIONS_CALL_TIMEOUT_SECONDS,
        )
        for entry in data.get("entries") or []:
            channel_id = entry.get("channel_id") or entry.get("id")
            if not channel_id or channel_id == own_channel_id:
                continue
            by_channel[channel_id] = {
                "youtube_channel_id": channel_id,
                "name": (
                    entry.get("channel")
                    or entry.get("title")
                    or entry.get("uploader")
                    or channel_id
                ),
                "handle": _entry_handle(entry),
                "avatar_url": None,
                "source": "featured",
                "score": FEATURED_SCORE,
            }
    except YoutubeResolveError as exc:
        logger.info("No featured channels for %s: %s", normalized, exc)

    # 2. Public playlists: count how often other channels appear.
    playlist_urls: list[str] = []
    if time.monotonic() < deadline:
        try:
            data = await asyncio.to_thread(
                _run_yt_dlp_json,
                _channel_url(normalized, "/playlists"),
                # Only the first few playlists are fetched below; bounding the
                # tab dump keeps this fast for channels with thousands.
                ["--playlist-items", f"1-{MAX_PLAYLISTS}"],
                SUGGESTIONS_CALL_TIMEOUT_SECONDS,
            )
            for entry in (data.get("entries") or [])[:MAX_PLAYLISTS]:
                url = entry.get("url")
                if not url and entry.get("id"):
                    url = f"https://www.youtube.com/playlist?list={entry['id']}"
                if url:
                    playlist_urls.append(url)
        except YoutubeResolveError as exc:
            logger.info("Could not list playlists for %s: %s", normalized, exc)

    counts: dict[str, int] = {}
    meta: dict[str, dict[str, Any]] = {}
    for playlist_url in playlist_urls:
        if time.monotonic() >= deadline:
            logger.info("Suggestion budget exhausted for %s", normalized)
            break
        try:
            data = await asyncio.to_thread(
                _run_yt_dlp_json,
                playlist_url,
                ["--playlist-items", f"1-{MAX_PLAYLIST_ITEMS}"],
                SUGGESTIONS_CALL_TIMEOUT_SECONDS,
            )
        except YoutubeResolveError as exc:
            logger.info("Skipping playlist %s: %s", playlist_url, exc)
            continue
        for entry in data.get("entries") or []:
            channel_id = entry.get("channel_id")
            if not channel_id or channel_id == own_channel_id:
                continue
            counts[channel_id] = counts.get(channel_id, 0) + 1
            if channel_id not in meta:
                meta[channel_id] = {
                    "name": entry.get("channel") or entry.get("uploader") or channel_id,
                    "handle": _entry_handle(entry),
                }

    for channel_id, count in counts.items():
        if channel_id in by_channel:
            continue  # already suggested via featured (higher score)
        by_channel[channel_id] = {
            "youtube_channel_id": channel_id,
            "name": meta[channel_id]["name"],
            "handle": meta[channel_id]["handle"],
            "avatar_url": None,
            "source": "playlists",
            "score": count,
        }

    suggestions = sorted(by_channel.values(), key=lambda s: -int(s["score"]))
    suggestions = suggestions[:MAX_SUGGESTIONS]
    _cache_set(_suggestions_cache, normalized, suggestions)
    return suggestions
