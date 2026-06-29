"""Shared yt-dlp invocation helpers.

The key one is :func:`cookie_args`: every yt-dlp call (resolve, download, preview,
transcript, channel import) splices these in so a configured YouTube cookies file
authenticates the request. Without it, age-restricted / members-only videos fail
extraction entirely ("Sign in to confirm your age"), so none of the playback
paths work for them.
"""

import os

from app.config import settings


def cookies_path() -> str | None:
    """Resolve the YouTube cookies file path, or None if none is present.

    Uses ``settings.youtube_cookies_file`` when set, otherwise the conventional
    ``<config_path>/cookies.txt`` (so dropping the file into the config volume is
    enough). Returns None when the resolved path doesn't exist on disk.
    """
    candidate = settings.youtube_cookies_file or os.path.join(
        settings.config_path, "cookies.txt"
    )
    return candidate if candidate and os.path.isfile(candidate) else None


def cookie_args() -> list[str]:
    """``["--cookies", <path>]`` when a cookies file is present, else ``[]``.

    Splice right after ``"yt-dlp"`` in any command list.
    """
    path = cookies_path()
    return ["--cookies", path] if path else []
