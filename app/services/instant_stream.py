"""Instant-start playback by reverse-proxying a progressive source stream.

A cold press on a video nobody has downloaded used to wait for the backend to
download a whole 360p preview file before the first frame. This module removes
that wait: it resolves a *progressive* (single-file, muxed audio+video) source
URL with yt-dlp and streams those bytes straight through the backend to the
client, forwarding Range requests. Playback starts as soon as the first bytes
arrive (~1-2s, dominated by the yt-dlp resolve), with no file written to disk.

Why proxy instead of redirecting the client to the source URL: the signed
source URLs are bound to the *server's* IP, so a 302 to the device gets 403s.
Fetching server-side keeps the URL valid; the backend is a byte pass-through
(bandwidth, no transcode).

The HQ download + seamless in-player swap is unchanged and orthogonal: this
just makes the instant tier instant. Resolved URLs are cached briefly (they
carry a multi-hour ``expire``), so repeat presses skip the resolve entirely.
"""

import asyncio
import logging
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.responses import StreamingResponse

from app.utils.unplayable import classify_extraction_error, extract_error_text
from app.utils.ytdlp import (
    PROGRESSIVE_FORMAT,
    cookie_args,
    player_client_args,
)

logger = logging.getLogger(__name__)

# yt-dlp resolve timeout. Resolve is a metadata/extraction round-trip, not a
# download, so it should be quick; keep it tight so a wedged extract surfaces as
# a 502 fast instead of hanging the player on a spinner.
_RESOLVE_TIMEOUT_SECONDS = 30

# Cap on time-to-first-byte from the upstream source. The body stream itself is
# left untimed (``read=None``) so long playback isn't interrupted, but a source
# that accepts the connection and then stalls before sending the response must
# NOT hang the request forever — that's what leaves the player spinning on
# "Preparing your video…". Surface it as a 502 so the client falls back.
_FIRST_BYTE_TIMEOUT_SECONDS = 20

# Fallback cache TTL when the source URL has no parseable ``expire``. Source
# URLs typically last ~6h; we re-resolve well before that.
_DEFAULT_TTL_SECONDS = 3600

# Re-resolve this many seconds before the URL's own expiry so we never hand a
# client a URL that dies mid-stream.
_EXPIRY_SAFETY_SECONDS = 300

_UPSTREAM_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Response headers worth forwarding from the source so the client gets correct
# length/range/type metadata for seeking.
_FORWARDED_HEADERS = ("content-length", "content-range", "content-type")

# video_id -> (url, expiry_epoch). Per-process; each worker resolves once and
# reuses. Cleared between tests via conftest's reset fixture.
_resolve_cache: dict[str, tuple[str, float]] = {}
_cache_lock = threading.Lock()


class InstantStreamError(Exception):
    """Raised when a progressive source URL cannot be resolved.

    ``reason`` carries the canonical unplayable reason (app/utils/unplayable)
    when the failure is inherent to the video — age gate, members-only,
    removed, … — and None for infrastructural/transient failures.
    """

    def __init__(self, message: str, reason: str | None = None):
        super().__init__(message)
        self.reason = reason


def _ytdlp_get_url(youtube_video_id: str) -> str:
    """Resolve a direct progressive media URL with ``yt-dlp -g`` (blocking).

    Runs in a worker thread (see :func:`resolve_progressive_url`). Raises
    :class:`InstantStreamError` on any failure so the caller can map it to 502.
    """
    url = f"https://www.youtube.com/watch?v={youtube_video_id}"
    cmd = [
        "yt-dlp",
        *cookie_args(),
        *player_client_args(),
        "--format",
        PROGRESSIVE_FORMAT,
        "--get-url",
        "--no-playlist",
        "--no-warnings",
        url,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_RESOLVE_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as exc:
        raise InstantStreamError(f"Resolve timed out for {youtube_video_id}") from exc

    if result.returncode != 0:
        stderr = result.stderr or result.stdout or ""
        # NB: deliberately do NOT flag cookie status here. With the android
        # client an age-restricted video age-gates even when the cookies are
        # valid (android can't use them; web passes age but is SABR-only), so a
        # resolve failure is not a reliable cookie signal. Cookie validity is
        # owned by the save-time verify (see app/utils/ytdlp.verify_cookies).
        detail = extract_error_text(stderr) or "unknown error"
        raise InstantStreamError(
            f"Resolve failed for {youtube_video_id}: {detail[:300]}",
            reason=classify_extraction_error(detail),
        )

    # -g prints one URL per selected stream; a progressive format yields exactly
    # one. Take the first non-empty line defensively.
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            return line
    raise InstantStreamError(f"Resolve returned no URL for {youtube_video_id}")


def _compute_expiry(url: str, now: float) -> float:
    """Cache-until epoch for a resolved URL, honouring its own ``expire`` param."""
    cap = now + _DEFAULT_TTL_SECONDS
    try:
        expire = parse_qs(urlparse(url).query).get("expire", [None])[0]
        if expire is not None:
            return min(cap, float(expire) - _EXPIRY_SAFETY_SECONDS)
    except (ValueError, TypeError):
        pass
    return cap


def resolve_progressive_url(youtube_video_id: str) -> str:
    """Return a cached or freshly resolved progressive source URL (blocking).

    Call via ``asyncio.to_thread`` from async code — it spawns yt-dlp. Raises
    :class:`InstantStreamError` if resolution fails.
    """
    now = time.time()
    with _cache_lock:
        cached = _resolve_cache.get(youtube_video_id)
        if cached is not None and cached[1] > now:
            return cached[0]

    url = _ytdlp_get_url(youtube_video_id)

    with _cache_lock:
        _resolve_cache[youtube_video_id] = (url, _compute_expiry(url, now))
    return url


def _make_client() -> httpx.AsyncClient:
    """Build the AsyncClient used to fetch the source.

    Factored out so tests can inject an ``httpx.MockTransport``. Read timeout is
    disabled (a stream stays open for the whole playback), while connect/write
    stay bounded so a dead source fails fast.
    """
    timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
    return httpx.AsyncClient(follow_redirects=True, timeout=timeout)


async def stream_proxy(url: str, range_header: str | None) -> StreamingResponse:
    """Reverse-proxy ``url`` to the client, forwarding Range and mirroring status.

    The source's status (200/206), Content-Length, Content-Range and
    Content-Type are passed through so the client's player can seek normally.
    The client and upstream response are closed when the body is fully streamed
    (or the client disconnects and the generator is torn down).
    """
    req_headers = {"User-Agent": _UPSTREAM_USER_AGENT}
    if range_header:
        req_headers["Range"] = range_header

    client = _make_client()
    try:
        request = client.build_request("GET", url, headers=req_headers)
        # Bound time-to-first-byte only; the body generator below streams
        # untimed so long playback isn't cut off.
        async with asyncio.timeout(_FIRST_BYTE_TIMEOUT_SECONDS):
            upstream = await client.send(request, stream=True)
    except (httpx.HTTPError, TimeoutError) as exc:
        await client.aclose()
        raise InstantStreamError(f"Upstream fetch failed: {exc}") from exc

    out_headers = {
        key: upstream.headers[key]
        for key in _FORWARDED_HEADERS
        if key in upstream.headers
    }
    out_headers["Accept-Ranges"] = "bytes"
    media_type = upstream.headers.get("content-type", "video/mp4")

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers=out_headers,
        media_type=media_type,
    )
