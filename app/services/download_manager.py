import logging
import os
import re
import shutil
import signal
import subprocess
import json
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable

import httpx

from app.config import settings
from app.utils.ytdlp import cookie_args, player_client_args

logger = logging.getLogger(__name__)

# YouTube publishes a per-channel Atom feed of the ~15 newest uploads, keyed by
# canonical UC channel id. Routine polling fetches this with a conditional GET
# instead of a heavyweight yt-dlp playlist scan; see fetch_channel_rss.
YOUTUBE_RSS_FEED_URL = "https://www.youtube.com/feeds/videos.xml"

# Namespaces in the channel Atom feed. ElementTree matches tags by their
# {namespace}localname form, so these are spelled out as prefixes.
_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_YT_NS = "{http://www.youtube.com/xml/schemas/2015}"

# --- Download watchdog tuning ---------------------------------------------
# A live yt-dlp/aria2c download prints stdout lines constantly while fetching,
# plus a short quiet spell around the post-download stream-copy merge. A gap
# longer than this means the process is wedged (dead socket, hung aria2c) rather
# than busy, so we kill it. Generous enough to cover a slow merge of a large
# file that produces no progress output.
NO_OUTPUT_TIMEOUT_SECONDS = 300

# Absolute ceiling on a single download regardless of progress. A legitimate
# large download on a slow link can run long, so this is deliberately generous;
# the Celery soft/hard time limits sit just above it as a coarser backstop.
OVERALL_DEADLINE_SECONDS = 4 * 3600

# How often the watchdog thread wakes to evaluate the timers and cancel flag.
WATCHDOG_POLL_INTERVAL_SECONDS = 5.0


class DownloadCancelled(Exception):
    """Raised when an in-flight download is cancelled (e.g. via the API)."""


def _kill_process_group(process: subprocess.Popen) -> None:
    """Kill a subprocess and its children (yt-dlp delegates to aria2c)."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        process.kill()


class _DownloadWatchdog(threading.Thread):
    """Kills a wedged download from a side thread.

    The main thread blocks reading yt-dlp stdout line by line, which on its own
    cannot detect a *silent* hang (process alive, socket dead, no output and no
    EOF). This thread watches three conditions and kills the process group when
    any trips; killing it closes the pipe and unblocks the reader. The reader
    then inspects ``reason`` to decide what to raise.

      * no stdout for ``no_output_timeout`` seconds -> "no_output"
      * total runtime past ``overall_deadline`` seconds -> "deadline"
      * ``cancel_check()`` returns True (cancelled via the API) -> "cancelled"
    """

    def __init__(
        self,
        process: subprocess.Popen,
        cancel_check: Callable[[], bool] | None = None,
        no_output_timeout: float = NO_OUTPUT_TIMEOUT_SECONDS,
        overall_deadline: float = OVERALL_DEADLINE_SECONDS,
        poll_interval: float = WATCHDOG_POLL_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(daemon=True)
        self._process = process
        self._cancel_check = cancel_check
        self._no_output_timeout = no_output_timeout
        self._overall_deadline = overall_deadline
        self._poll_interval = poll_interval
        self._started_at = time.monotonic()
        self._last_output_at = self._started_at
        # NB: must not be named ``_stop`` — that shadows ``threading.Thread._stop``,
        # an internal method ``Thread.join()`` calls, which would raise
        # "'Event' object is not callable" on some CPython versions.
        self._stop_event = threading.Event()
        self.reason: str | None = None

    def note_output(self) -> None:
        """Record that the reader just saw a line (resets the stall timer)."""
        self._last_output_at = time.monotonic()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.wait(self._poll_interval):
            if self._process.poll() is not None:
                return  # process exited on its own; the reader is draining EOF
            now = time.monotonic()
            if self._cancel_check is not None:
                try:
                    cancelled = self._cancel_check()
                except Exception:
                    cancelled = False
                if cancelled:
                    self.reason = "cancelled"
                    _kill_process_group(self._process)
                    return
            if now - self._last_output_at > self._no_output_timeout:
                self.reason = "no_output"
                _kill_process_group(self._process)
                return
            if now - self._started_at > self._overall_deadline:
                self.reason = "deadline"
                _kill_process_group(self._process)
                return


def download_video(
    youtube_video_id: str,
    channel_slug: str,
    quality: str | None = None,
    progress_callback: Callable[[float], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    heartbeat_callback: Callable[[], None] | None = None,
) -> dict:
    """
    Download a video using yt-dlp. Returns metadata dict on success.

    A watchdog thread runs alongside the stdout reader and kills yt-dlp if the
    download stalls (no output for NO_OUTPUT_TIMEOUT_SECONDS), runs past
    OVERALL_DEADLINE_SECONDS, or is cancelled. `cancel_check` is polled every
    ~5s by that thread, so cancellation works even during a silent hang; when
    it returns True, yt-dlp is killed, partial files are cleaned up, and
    DownloadCancelled is raised.

    `heartbeat_callback` (if given) is invoked on every stdout line so the
    caller can record a liveness timestamp the reaper can use to detect a
    crashed worker.

    Raises RuntimeError on failure or stall, DownloadCancelled on cancellation.
    """
    quality = quality or settings.media_quality
    output_dir = os.path.join(settings.media_path, channel_slug)
    os.makedirs(output_dir, exist_ok=True)

    output_template = os.path.join(output_dir, f"{youtube_video_id}.%(ext)s")

    # Map quality setting to yt-dlp format string
    # Prefer H.264 (avc1) video + AAC (mp4a) audio for browser compatibility.
    # Fallback chain ensures we still get something if H.264+AAC isn't available.
    format_map = {
        "720p": "bestvideo[height<=720][vcodec^=avc1]+bestaudio[acodec^=mp4a]/best[height<=720][vcodec^=avc1]/best[height<=720]",
        "1080p": "bestvideo[height<=1080][vcodec^=avc1]+bestaudio[acodec^=mp4a]/best[height<=1080][vcodec^=avc1]/best[height<=1080]",
        "4k": "bestvideo[height<=2160][vcodec^=avc1]+bestaudio[acodec^=mp4a]/best[height<=2160][vcodec^=avc1]/best[height<=2160]",
        "best": "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/best[vcodec^=avc1]/best",
    }
    format_str = format_map.get(quality, format_map["1080p"])

    url = f"https://www.youtube.com/watch?v={youtube_video_id}"

    cmd = [
        "yt-dlp",
        *cookie_args(),
        # Default clients (tv/web_creator) get downgraded to storyboards-only for
        # age-restricted videos; the web/android clients return the real formats.
        *player_client_args(),
        "--format",
        format_str,
        "--merge-output-format",
        "mp4",
        "--output",
        output_template,
        "--write-info-json",
        "--write-thumbnail",
        "--no-playlist",
        "--retries",
        "3",
        "--no-overwrites",
        "--newline",
        "--downloader",
        "aria2c",
        url,
    ]

    logger.info("Starting download: %s", youtube_video_id)

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,  # line-buffered for more frequent progress updates
        start_new_session=True,  # own process group so cancel kills aria2c too
    )

    # yt-dlp native: [download]  45.2% ...
    # aria2c:        [#abc 1.7MiB/81MiB(2%) ...]
    progress_re = re.compile(r"\[download\]\s+([\d.]+)%|\((\d+)%\)")
    last_callback_time = 0.0
    last_line = ""

    # Read the tuning constants at call time (not via the watchdog's default
    # args, which bind at import) so they can be overridden in tests.
    watchdog = _DownloadWatchdog(
        process,
        cancel_check=cancel_check,
        no_output_timeout=NO_OUTPUT_TIMEOUT_SECONDS,
        overall_deadline=OVERALL_DEADLINE_SECONDS,
        poll_interval=WATCHDOG_POLL_INTERVAL_SECONDS,
    )
    watchdog.start()
    stdout_lines = iter(process.stdout.readline, "") if process.stdout else iter(())
    try:
        for line in stdout_lines:
            watchdog.note_output()
            last_line = line

            if heartbeat_callback is not None:
                heartbeat_callback()

            m = progress_re.search(line)
            if m and progress_callback is not None:
                now = time.monotonic()
                if now - last_callback_time >= 2.0:
                    last_callback_time = now
                    pct = float(m.group(1) or m.group(2))
                    progress_callback(pct)

        # The reader saw EOF: either yt-dlp finished or the watchdog killed it.
        # The process is already exiting, so this wait should return promptly.
        process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        process.wait()
        raise RuntimeError(
            f"yt-dlp did not exit after stream close: {youtube_video_id}"
        )
    finally:
        watchdog.stop()
        watchdog.join(timeout=10)
        # Belt and suspenders: never leave yt-dlp/aria2c running if we bail out
        # for any reason the loop above didn't handle (e.g. a Celery time-limit
        # signal raising through the reader).
        if process.poll() is None:
            _kill_process_group(process)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    if watchdog.reason == "cancelled":
        _cleanup_partial_files(output_dir, youtube_video_id)
        raise DownloadCancelled(f"Download cancelled for {youtube_video_id}")
    if watchdog.reason == "no_output":
        _cleanup_partial_files(output_dir, youtube_video_id)
        raise RuntimeError(
            f"Download stalled (no output for {NO_OUTPUT_TIMEOUT_SECONDS}s): "
            f"{youtube_video_id}"
        )
    if watchdog.reason == "deadline":
        _cleanup_partial_files(output_dir, youtube_video_id)
        raise RuntimeError(
            f"Download exceeded {OVERALL_DEADLINE_SECONDS}s deadline: "
            f"{youtube_video_id}"
        )

    if process.returncode != 0:
        logger.error("yt-dlp failed for %s: %s", youtube_video_id, last_line)
        raise RuntimeError(f"yt-dlp failed: {last_line[:500]}")

    # Find the downloaded file
    file_path = _find_downloaded_file(output_dir, youtube_video_id)
    if not file_path:
        raise RuntimeError(f"Downloaded file not found for {youtube_video_id}")

    # Parse metadata from info JSON
    metadata = _load_info_json(output_dir, youtube_video_id)

    # Copy thumbnail to thumbnails directory
    _copy_thumbnail(output_dir, youtube_video_id)

    file_size = os.path.getsize(file_path)
    relative_path = os.path.relpath(file_path, settings.media_path)

    return {
        "file_path": relative_path,
        "file_size_bytes": file_size,
        "title": metadata.get("title", youtube_video_id),
        "duration_seconds": int(metadata.get("duration", 0)),
        "uploaded_at": metadata.get("upload_date"),
        "metadata_json": metadata,
    }


def download_preview(
    youtube_video_id: str,
    channel_slug: str,
    video_id: str,
) -> dict:
    """
    Download a low-quality (~360-480p) preview. Returns metadata dict on success.

    Prefers a pre-muxed progressive file (fast, no merge). Falls back to muxing a
    ≤480p video+audio pair for videos YouTube only serves as SABR/adaptive —
    notably age-restricted ones, which the bundled po_token provider makes
    downloadable but which have no progressive stream, so this fallback is the
    only way they get a playable file (and thus the only cold-press path that
    works for them). Raises RuntimeError on failure.
    """
    output_dir = os.path.join(settings.media_path, channel_slug)
    os.makedirs(output_dir, exist_ok=True)

    output_template = os.path.join(output_dir, f"{video_id}_preview.%(ext)s")

    # Progressive first (no merge); then an adaptive video+audio pair to mux for
    # SABR-only videos (age-restricted) that have no progressive format. Prefer
    # 360p for the adaptive branch — it's the cold-press preview, so smaller and
    # faster matters more than resolution.
    format_str = (
        "best[height<=360][ext=mp4]"
        "/bestvideo[height<=360][vcodec^=avc1]+bestaudio[acodec^=mp4a]"
        "/bestvideo[height<=480][vcodec^=avc1]+bestaudio[acodec^=mp4a]"
        "/bestvideo[height<=720][vcodec^=avc1]+bestaudio[acodec^=mp4a]"
        "/bestvideo[height<=720][vcodec^=avc1]+bestaudio"  # any audio if no m4a
        "/best[height<=480][ext=mp4]"
        "/worst[ext=mp4]"
    )

    url = f"https://www.youtube.com/watch?v={youtube_video_id}"

    cmd = [
        "yt-dlp",
        *cookie_args(),
        # Default clients (tv/web_creator) get downgraded to storyboards-only for
        # age-restricted videos; the web/android clients return the real formats.
        *player_client_args(),
        "--format",
        format_str,
        # Only takes effect when the adaptive fallback is selected (a merge);
        # harmless for the progressive branch.
        "--merge-output-format",
        "mp4",
        # Parallel download (like the HQ path). A single-connection pull of a
        # long adaptive stream is too slow and times out; aria2c keeps even a
        # 2h+ episode well inside the cap so the preview is ready quickly.
        "--downloader",
        "aria2c",
        "--output",
        output_template,
        "--no-playlist",
        "--retries",
        "3",
        "--no-overwrites",
        url,
    ]

    logger.info("Starting preview download: %s", youtube_video_id)

    # subprocess.run drains stdout/stderr while waiting (via communicate),
    # avoiding the pipe-fill deadlock of Popen + wait with a PIPE attached.
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            # Muxing an adaptive pair (the age-restricted path) downloads the
            # full A/V streams; with aria2c this is quick, but allow generous
            # headroom for long episodes / slow links before giving up.
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Preview download timed out for {youtube_video_id}")

    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-1:]
        detail = tail[0] if tail else "unknown error"
        # "Requested format is not available" usually means the po_token provider
        # is down (authenticated SABR formats stay hidden). Dump what yt-dlp does
        # see so we can tell "no formats at all" (po_token) from "selector miss".
        if "format is not available" in detail.lower():
            try:
                probe = subprocess.run(
                    [
                        "yt-dlp",
                        *cookie_args(),
                        *player_client_args(),
                        "-F",
                        "--no-warnings",
                        "--no-playlist",
                        url,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                logger.warning(
                    "Available formats for %s (diagnostic):\n%s",
                    youtube_video_id,
                    (probe.stdout or probe.stderr or "(none)").strip()[-1800:],
                )
            except Exception:
                logger.warning("Could not list formats for %s", youtube_video_id)
        raise RuntimeError(
            f"Preview download failed for {youtube_video_id}: {detail[:300]}"
        )

    file_path = _find_preview_file(output_dir, video_id)
    if not file_path:
        raise RuntimeError(f"Preview file not found for {youtube_video_id}")

    file_size = os.path.getsize(file_path)
    relative_path = os.path.relpath(file_path, settings.media_path)

    return {
        "file_path": relative_path,
        "file_size_bytes": file_size,
    }


def fetch_transcript(youtube_video_id: str) -> list[dict] | None:
    """Fetch the timestamped English transcript (auto-captions) via yt-dlp.

    Returns a list of ``{"start": float_seconds, "text": str}`` cues, or None if
    no captions are available or extraction fails. Used as input to AI ad-segment
    detection — it downloads only the subtitle track (``--skip-download``), parsed
    from yt-dlp's json3 format.
    """
    url = f"https://www.youtube.com/watch?v={youtube_video_id}"
    with tempfile.TemporaryDirectory() as tmp:
        output_template = os.path.join(tmp, "%(id)s.%(ext)s")
        cmd = [
            "yt-dlp",
            *cookie_args(),
            "--skip-download",
            "--write-auto-subs",
            "--write-subs",
            "--sub-langs",
            "en.*",
            "--sub-format",
            "json3",
            "--no-playlist",
            "--output",
            output_template,
            url,
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            logger.warning("Transcript fetch timed out for %s", youtube_video_id)
            return None

        json3_files = [f for f in os.listdir(tmp) if f.endswith(".json3")]
        if not json3_files:
            return None
        try:
            with open(os.path.join(tmp, json3_files[0])) as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    cues: list[dict] = []
    for event in data.get("events", []):
        segs = event.get("segs")
        start_ms = event.get("tStartMs")
        if not segs or start_ms is None:
            continue
        text = "".join(seg.get("utf8", "") for seg in segs).strip()
        if text:
            cues.append({"start": round(start_ms / 1000.0, 2), "text": text})
    return cues or None


def _cleanup_partial_files(output_dir: str, youtube_video_id: str) -> None:
    """Remove partial download artifacts for a video in the channel directory.

    Targets `.part`, `.aria2`, `.ytdl` files and `.fNNN.` format fragments
    left behind by an interrupted yt-dlp/aria2c download.
    """
    try:
        entries = os.listdir(output_dir)
    except OSError:
        return
    fragment_re = re.compile(rf"^{re.escape(youtube_video_id)}\.f\d+\.")
    for name in entries:
        if not name.startswith(youtube_video_id):
            continue
        if name.endswith((".part", ".aria2", ".ytdl")) or fragment_re.match(name):
            path = os.path.join(output_dir, name)
            try:
                os.remove(path)
                logger.info("Removed partial file: %s", path)
            except OSError:
                logger.warning("Failed to remove partial file: %s", path)


def _find_preview_file(output_dir: str, video_id: str) -> str | None:
    """Find the downloaded preview file in the output directory."""
    for f in os.listdir(output_dir):
        if f.startswith(f"{video_id}_preview") and not f.endswith(".part"):
            return os.path.join(output_dir, f)
    return None


def _find_downloaded_file(output_dir: str, video_id: str) -> str | None:
    """Find the downloaded video file in the output directory."""
    for f in os.listdir(output_dir):
        if f.startswith(video_id) and not f.endswith(
            (".json", ".jpg", ".webp", ".png", ".part")
        ):
            return os.path.join(output_dir, f)
    return None


def _load_info_json(output_dir: str, video_id: str) -> dict:
    """Load the yt-dlp info JSON file."""
    info_path = os.path.join(output_dir, f"{video_id}.info.json")
    if os.path.exists(info_path):
        with open(info_path) as f:
            return json.load(f)
    return {}


def _copy_thumbnail(output_dir: str, video_id: str) -> None:
    """Copy thumbnail to the thumbnails directory."""
    thumb_dir = settings.thumbnails_path
    os.makedirs(thumb_dir, exist_ok=True)
    dest = os.path.join(thumb_dir, f"{video_id}.jpg")

    if os.path.exists(dest):
        return

    # yt-dlp may save as .webp, .jpg, or .png
    for ext in ("jpg", "webp", "png"):
        src = os.path.join(output_dir, f"{video_id}.{ext}")
        if os.path.exists(src):
            if ext == "jpg":
                if not os.path.exists(dest):
                    try:
                        os.link(src, dest)
                    except OSError:
                        # Fallback to copy if hardlink fails (e.g., different filesystems in Docker)
                        shutil.copy2(src, dest)
            else:
                # Convert to jpg using ffmpeg
                subprocess.run(
                    ["ffmpeg", "-i", src, "-y", dest],
                    capture_output=True,
                    timeout=30,
                )
            return


def _build_channel_url(youtube_channel_id: str, suffix: str = "") -> str:
    """Build a YouTube channel URL from an ID or handle."""
    if youtube_channel_id.startswith("@"):
        return f"https://www.youtube.com/{youtube_channel_id}{suffix}"
    elif youtube_channel_id.startswith("UC"):
        return f"https://www.youtube.com/channel/{youtube_channel_id}{suffix}"
    else:
        return f"https://www.youtube.com/@{youtube_channel_id}{suffix}"


def fetch_channel_metadata(youtube_channel_id: str) -> dict:
    """Fetch channel metadata using yt-dlp.

    Uses the /videos playlist page and reads playlist_* fields from the first
    entry, which reliably returns the channel name, canonical UC ID, and
    @handle for any input format.
    """
    url = _build_channel_url(youtube_channel_id, "/videos")

    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-json",
        "--playlist-items",
        "1",
        url,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout.strip().split("\n")[0])
            name = (
                data.get("playlist_channel")
                or data.get("playlist_uploader")
                or data.get("channel")
                or data.get("uploader")
                or youtube_channel_id
            )
            canonical_id = (
                data.get("playlist_channel_id")
                or data.get("channel_id")
                or youtube_channel_id
            )
            handle = data.get("playlist_uploader_id")  # e.g. "@KillTony"
            return {
                "name": name,
                "description": data.get("description", ""),
                "channel_id": canonical_id,
                "handle": handle,
            }
    except Exception as e:
        logger.warning(
            "Failed to fetch channel metadata for %s: %s", youtube_channel_id, e
        )

    return {
        "name": youtube_channel_id,
        "description": "",
        "channel_id": youtube_channel_id,
        "handle": None,
    }


def fetch_channel_images(youtube_channel_id: str) -> dict:
    """Fetch channel avatar and banner image URLs from the YouTube channel page.

    Returns dict with 'avatar_url' and 'banner_url' (either may be None).
    """
    url = _build_channel_url(youtube_channel_id)

    try:
        resp = httpx.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
            cookies={"CONSENT": "YES+1"},
            follow_redirects=True,
            timeout=15,
        )
        html = resp.text

        avatar_url = None
        banner_url = None

        # Avatar: extract from og:image meta tag (reliable across page versions)
        m = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
        if m:
            avatar_url = m.group(1)

        # Banner: search for banner thumbnails in page data
        for marker in ('"banner":{"thumbnails"', '"banner":{"imageBannerViewModel"'):
            pos = html.find(marker)
            if pos >= 0:
                segment = html[pos : pos + 3000]
                m_list = re.search(r"\[(.*?)\]", segment)
                if m_list:
                    urls = re.findall(
                        r'"(https://yt3\.(?:ggpht|googleusercontent)\.com/[^"]+)"',
                        m_list.group(1),
                    )
                    if urls:
                        # Last URL is typically the highest resolution
                        banner_url = urls[-1].replace("\\u0026", "&")
                    break

        logger.info(
            "Channel images for %s: avatar=%s, banner=%s",
            youtube_channel_id,
            bool(avatar_url),
            bool(banner_url),
        )
        return {"avatar_url": avatar_url, "banner_url": banner_url}

    except Exception as e:
        logger.warning(
            "Failed to fetch channel images for %s: %s", youtube_channel_id, e
        )
        return {"avatar_url": None, "banner_url": None}


def fetch_channel_videos(
    youtube_channel_id: str, max_videos: int | None = None
) -> dict:
    """Fetch the latest video IDs from a channel using yt-dlp.

    Returns a dict with 'videos' list and 'channel_meta' with resolved
    channel name / canonical UC ID / handle from the playlist fields.

    ``max_videos`` defaults to ``settings.catalog_fetch_count`` — the size of
    the back catalog ingested on a channel's first poll. Routine polls no longer
    use this path (they use the RSS feed), so this primarily bounds the initial
    catalog.
    """
    if max_videos is None:
        max_videos = settings.catalog_fetch_count
    url = _build_channel_url(youtube_channel_id, "/videos")

    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-json",
        "--playlist-items",
        f"1:{max_videos}",
        url,
    ]

    videos = []
    channel_meta = None
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                data = json.loads(line)
                videos.append(
                    {
                        "youtube_video_id": data.get("id", ""),
                        "title": data.get("title", ""),
                        "duration_seconds": int(data.get("duration") or 0),
                        "upload_date": data.get("upload_date"),
                    }
                )
                # Extract channel metadata from the first entry
                if channel_meta is None:
                    channel_meta = {
                        "name": (
                            data.get("playlist_channel")
                            or data.get("playlist_uploader")
                            or data.get("channel")
                            or data.get("uploader")
                        ),
                        "channel_id": (
                            data.get("playlist_channel_id") or data.get("channel_id")
                        ),
                        "handle": data.get("playlist_uploader_id"),
                    }
    except Exception as e:
        logger.warning("Failed to fetch videos for %s: %s", youtube_channel_id, e)

    return {"videos": videos, "channel_meta": channel_meta}


def _parse_rss_entries(xml_text: str) -> list[dict]:
    """Parse a YouTube channel Atom feed into newest-first video entries.

    Each entry yields ``youtube_video_id`` (the ``yt:videoId``), ``title`` and
    ``published`` (raw ISO-8601 string, or None). The feed is already ordered
    newest-first; we preserve that order.

    The body comes from YouTube over HTTPS (a trusted source), so stdlib
    ElementTree is used directly — no external-entity or untrusted-XML concerns
    that would warrant a third-party parser.
    """
    root = ET.fromstring(xml_text)
    entries: list[dict] = []
    for entry in root.findall(f"{_ATOM_NS}entry"):
        vid_el = entry.find(f"{_YT_NS}videoId")
        video_id = (vid_el.text or "").strip() if vid_el is not None else ""
        if not video_id:
            continue
        title_el = entry.find(f"{_ATOM_NS}title")
        published_el = entry.find(f"{_ATOM_NS}published")
        entries.append(
            {
                "youtube_video_id": video_id,
                "title": (title_el.text or "").strip() if title_el is not None else "",
                "published": published_el.text if published_el is not None else None,
            }
        )
    return entries


def fetch_channel_rss(
    youtube_channel_id: str,
    etag: str | None = None,
    last_modified: str | None = None,
) -> dict:
    """Fetch a channel's Atom upload feed with an HTTP conditional GET.

    Returns a dict whose ``status`` is one of:

      * ``"not_modified"`` — server replied 304; nothing changed since the
        stored validators, so the caller can short-circuit the poll entirely.
      * ``"ok"`` — a fresh feed; also carries ``entries`` (newest-first) and the
        ``etag`` / ``last_modified`` response validators to persist.
      * ``"unavailable"`` — RSS can't be used for this channel (the id isn't a
        canonical UC id, the request failed, or the body didn't parse); the
        caller should fall back to the yt-dlp listing.

    The feed is addressable only by canonical UC id; handles/legacy usernames
    aren't, so those report ``"unavailable"`` without a network call. Channels
    are canonicalized to UC ids by the metadata-refresh job, so they converge
    onto the cheap RSS path over time.
    """
    if not youtube_channel_id.startswith("UC"):
        return {"status": "unavailable"}

    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    url = f"{YOUTUBE_RSS_FEED_URL}?channel_id={youtube_channel_id}"
    try:
        resp = httpx.get(url, headers=headers, timeout=15, follow_redirects=True)
    except Exception as e:
        logger.warning("RSS fetch failed for %s: %s", youtube_channel_id, e)
        return {"status": "unavailable"}

    if resp.status_code == 304:
        return {"status": "not_modified"}

    if resp.status_code != 200:
        logger.warning(
            "RSS fetch for %s returned HTTP %s", youtube_channel_id, resp.status_code
        )
        return {"status": "unavailable"}

    try:
        entries = _parse_rss_entries(resp.text)
    except Exception as e:
        logger.warning("Failed to parse RSS for %s: %s", youtube_channel_id, e)
        return {"status": "unavailable"}

    return {
        "status": "ok",
        "entries": entries,
        "etag": resp.headers.get("ETag"),
        "last_modified": resp.headers.get("Last-Modified"),
    }


def fetch_videos_metadata(video_ids: list[str]) -> list[dict]:
    """Fetch yt-dlp metadata for specific video IDs, preserving input order.

    Used after RSS discovery surfaces genuinely-new uploads: only those IDs are
    extracted (typically one or two), instead of re-scanning the whole channel.
    Returns dicts shaped like ``fetch_channel_videos`` entries; IDs that fail to
    extract (private, removed, geo-blocked) are simply omitted.
    """
    if not video_ids:
        return []

    urls = [f"https://www.youtube.com/watch?v={vid}" for vid in video_ids]
    cmd = ["yt-dlp", "--dump-json", "--no-playlist", *urls]

    by_id: dict[str, dict] = {}
    try:
        # One video may fail (returncode != 0) while others succeed, so parse
        # whatever JSON lines came back regardless of the exit status.
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            vid = data.get("id", "")
            if vid:
                by_id[vid] = {
                    "youtube_video_id": vid,
                    "title": data.get("title", ""),
                    "duration_seconds": int(data.get("duration") or 0),
                    "upload_date": data.get("upload_date"),
                }
        if result.returncode != 0:
            logger.warning(
                "yt-dlp metadata fetch returned %s for %d id(s); got %d",
                result.returncode,
                len(video_ids),
                len(by_id),
            )
    except Exception as e:
        logger.warning("Failed to fetch metadata for %s: %s", video_ids, e)

    # Preserve the requested (newest-first) order; drop any that didn't resolve.
    return [by_id[vid] for vid in video_ids if vid in by_id]
