import logging
import os
import subprocess
import json

from app.config import settings

logger = logging.getLogger(__name__)


def download_video(
    youtube_video_id: str,
    channel_slug: str,
    quality: str | None = None,
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
    format_map = {
        "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        "4k": "bestvideo[height<=2160]+bestaudio/best[height<=2160]",
        "best": "bestvideo+bestaudio/best",
    }
    format_str = format_map.get(quality, format_map["1080p"])

    url = f"https://www.youtube.com/watch?v={youtube_video_id}"

    cmd = [
        "yt-dlp",
        "--format", format_str,
        "--merge-output-format", "mp4",
        "--output", output_template,
        "--write-info-json",
        "--write-thumbnail",
        "--no-playlist",
        "--retries", "3",
        "--no-overwrites",
        url,
    ]

    logger.info("Starting download: %s", youtube_video_id)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=3600,
    )

    if result.returncode != 0:
        logger.error("yt-dlp failed for %s: %s", youtube_video_id, result.stderr)
        raise RuntimeError(f"yt-dlp failed: {result.stderr[:500]}")

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


def _find_downloaded_file(output_dir: str, video_id: str) -> str | None:
    """Find the downloaded video file in the output directory."""
    for f in os.listdir(output_dir):
        if f.startswith(video_id) and not f.endswith((".json", ".jpg", ".webp", ".png", ".part")):
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
                os.link(src, dest) if not os.path.exists(dest) else None
            else:
                # Convert to jpg using ffmpeg
                subprocess.run(
                    ["ffmpeg", "-i", src, "-y", dest],
                    capture_output=True,
                    timeout=30,
                )
            return


def fetch_channel_metadata(youtube_channel_id: str) -> dict:
    """Fetch channel metadata using yt-dlp."""
    # Handle both channel IDs and @handles
    if youtube_channel_id.startswith("@"):
        url = f"https://www.youtube.com/{youtube_channel_id}"
    elif youtube_channel_id.startswith("UC"):
        url = f"https://www.youtube.com/channel/{youtube_channel_id}"
    else:
        url = f"https://www.youtube.com/@{youtube_channel_id}"

    cmd = [
        "yt-dlp",
        "--dump-json",
        "--playlist-items", "0",
        "--flat-playlist",
        url,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout.strip().split("\n")[0])
            return {
                "name": data.get("channel", data.get("uploader", youtube_channel_id)),
                "description": data.get("description", ""),
                "channel_id": data.get("channel_id", youtube_channel_id),
            }
    except Exception as e:
        logger.warning("Failed to fetch channel metadata for %s: %s", youtube_channel_id, e)

    return {"name": youtube_channel_id, "description": "", "channel_id": youtube_channel_id}


def fetch_channel_videos(youtube_channel_id: str, max_videos: int = 10) -> list[dict]:
    """Fetch the latest video IDs from a channel using yt-dlp."""
    if youtube_channel_id.startswith("@"):
        url = f"https://www.youtube.com/{youtube_channel_id}/videos"
    elif youtube_channel_id.startswith("UC"):
        url = f"https://www.youtube.com/channel/{youtube_channel_id}/videos"
    else:
        url = f"https://www.youtube.com/@{youtube_channel_id}/videos"

    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-json",
        "--playlist-items", f"1:{max_videos}",
        url,
    ]

    videos = []
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    data = json.loads(line)
                    videos.append({
                        "youtube_video_id": data.get("id", ""),
                        "title": data.get("title", ""),
                        "duration_seconds": int(data.get("duration") or 0),
                    })
    except Exception as e:
        logger.warning("Failed to fetch videos for %s: %s", youtube_channel_id, e)

    return videos
