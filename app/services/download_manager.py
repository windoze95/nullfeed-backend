import logging
import os
import re
import shutil
import subprocess
import json
import time
from collections.abc import Callable

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def download_video(
    youtube_video_id: str,
    channel_slug: str,
    quality: str | None = None,
    progress_callback: Callable[[float], None] | None = None,
) -> dict:
    """
    Download a video using yt-dlp. Returns metadata dict on success.
    Raises RuntimeError on failure.
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
    )

    # yt-dlp native: [download]  45.2% ...
    # aria2c:        [#abc 1.7MiB/81MiB(2%) ...]
    progress_re = re.compile(r"\[download\]\s+([\d.]+)%|\((\d+)%\)")
    last_callback_time = 0.0
    last_line = ""

    try:
        for line in process.stdout or []:
            last_line = line
            m = progress_re.search(line)
            if m and progress_callback is not None:
                now = time.monotonic()
                if now - last_callback_time >= 2.0:
                    last_callback_time = now
                    pct = float(m.group(1) or m.group(2))
                    progress_callback(pct)

        process.wait(timeout=3600)
    except subprocess.TimeoutExpired:
        process.kill()
        raise RuntimeError(f"yt-dlp timed out for {youtube_video_id}")

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
    Download a low-quality pre-muxed 360p preview. Returns metadata dict on success.
    No split streams, no merge step — fast and lightweight.
    Raises RuntimeError on failure.
    """
    output_dir = os.path.join(settings.media_path, channel_slug)
    os.makedirs(output_dir, exist_ok=True)

    output_template = os.path.join(output_dir, f"{video_id}_preview.%(ext)s")

    # Pre-muxed formats only — no merge needed
    format_str = "best[height<=360][ext=mp4]/best[height<=480][ext=mp4]/worst[ext=mp4]"

    url = f"https://www.youtube.com/watch?v={youtube_video_id}"

    cmd = [
        "yt-dlp",
        "--format",
        format_str,
        "--output",
        output_template,
        "--no-playlist",
        "--retries",
        "3",
        "--no-overwrites",
        url,
    ]

    logger.info("Starting preview download: %s", youtube_video_id)

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        process.wait(timeout=120)
    except subprocess.TimeoutExpired:
        process.kill()
        raise RuntimeError(f"Preview download timed out for {youtube_video_id}")

    if process.returncode != 0:
        raise RuntimeError(f"Preview download failed for {youtube_video_id}")

    file_path = _find_preview_file(output_dir, video_id)
    if not file_path:
        raise RuntimeError(f"Preview file not found for {youtube_video_id}")

    file_size = os.path.getsize(file_path)
    relative_path = os.path.relpath(file_path, settings.media_path)

    return {
        "file_path": relative_path,
        "file_size_bytes": file_size,
    }


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


def fetch_channel_videos(youtube_channel_id: str, max_videos: int = 50) -> dict:
    """Fetch the latest video IDs from a channel using yt-dlp.

    Returns a dict with 'videos' list and 'channel_meta' with resolved
    channel name / canonical UC ID / handle from the playlist fields.
    """
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
