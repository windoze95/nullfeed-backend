import mimetypes
import os
import hashlib
from datetime import datetime, timezone
from email.utils import formatdate
from time import mktime

from fastapi.responses import Response


def build_range_response(file_path: str, range_header: str) -> Response:
    """
    Build an HTTP 206 Partial Content response for range requests.
    Supports single byte ranges (e.g. "bytes=0-1023").
    """
    file_size = os.path.getsize(file_path)
    stat = os.stat(file_path)

    # Parse range header: "bytes=start-end"
    range_spec = range_header.replace("bytes=", "").strip()
    parts = range_spec.split("-")

    start = int(parts[0]) if parts[0] else 0
    end = int(parts[1]) if len(parts) > 1 and parts[1] else file_size - 1

    # Clamp values
    start = max(0, start)
    end = min(end, file_size - 1)

    if start > end or start >= file_size:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    content_length = end - start + 1

    # Read the requested range
    with open(file_path, "rb") as f:
        f.seek(start)
        data = f.read(content_length)

    content_type = _guess_content_type(file_path)
    etag = _compute_etag(file_path, stat)
    last_modified = _format_http_date(stat.st_mtime)

    return Response(
        content=data,
        status_code=206,
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(content_length),
            "Content-Type": content_type,
            "Accept-Ranges": "bytes",
            "ETag": etag,
            "Last-Modified": last_modified,
            "Cache-Control": "public, max-age=86400",
        },
    )


def _guess_content_type(file_path: str) -> str:
    mime, _ = mimetypes.guess_type(file_path)
    return mime or "application/octet-stream"


def _compute_etag(file_path: str, stat: os.stat_result) -> str:
    raw = f"{file_path}:{stat.st_size}:{stat.st_mtime}"
    return f'"{hashlib.md5(raw.encode()).hexdigest()}"'


def _format_http_date(timestamp: float) -> str:
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return formatdate(mktime(dt.timetuple()), usegmt=True)
