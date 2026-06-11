import asyncio
import hashlib
import mimetypes
import os
from collections.abc import AsyncIterator
from email.utils import formatdate

from fastapi.responses import Response, StreamingResponse

# Read media files in 1 MiB chunks so large files/ranges are never fully buffered.
CHUNK_SIZE = 1024 * 1024


def build_media_response(file_path: str, range_header: str | None = None) -> Response:
    """Build a streaming HTTP response for a media file.

    Implements RFC 7233 byte ranges:
    - No Range header -> 200 streaming the full file.
    - `bytes=N-`, `bytes=N-M`, suffix `bytes=-N` (last N bytes) -> 206.
    - Multi-range requests are served as the first range only.
    - Malformed or unsatisfiable ranges -> 416 with `Content-Range: bytes */size`.

    ETag (derived from mtime+size) and Last-Modified are set on both the
    200 and 206 paths. File content is streamed in chunks via a thread
    offload — the requested range is never read into memory at once.
    """
    stat = os.stat(file_path)
    file_size = stat.st_size

    headers = {
        "Accept-Ranges": "bytes",
        "ETag": _compute_etag(stat),
        "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
        "Cache-Control": "public, max-age=86400",
    }
    content_type = _guess_content_type(file_path)

    if range_header is None:
        headers["Content-Length"] = str(file_size)
        return StreamingResponse(
            _file_iterator(file_path, 0, file_size),
            status_code=200,
            media_type=content_type,
            headers=headers,
        )

    byte_range = _parse_range(range_header, file_size)
    if byte_range is None:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    start, end = byte_range
    content_length = end - start + 1
    headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    headers["Content-Length"] = str(content_length)
    return StreamingResponse(
        _file_iterator(file_path, start, content_length),
        status_code=206,
        media_type=content_type,
        headers=headers,
    )


def _parse_range(range_header: str, file_size: int) -> tuple[int, int] | None:
    """Parse a Range header into an inclusive (start, end) byte tuple.

    Returns None when the header is malformed or unsatisfiable; the caller
    responds with 416. Multi-range requests yield the first range only.
    """
    header = range_header.strip()
    if file_size <= 0 or not header.lower().startswith("bytes="):
        return None

    # Multi-range: serve the first range only.
    spec = header[len("bytes=") :].split(",")[0].strip()
    if "-" not in spec:
        return None
    start_s, _, end_s = spec.partition("-")
    start_s = start_s.strip()
    end_s = end_s.strip()

    if start_s:
        # Open-ended "N-" or bounded "N-M".
        if not start_s.isdigit():
            return None
        start = int(start_s)
        if start >= file_size:
            return None
        if not end_s:
            return start, file_size - 1
        if not end_s.isdigit():
            return None
        end = int(end_s)
        if end < start:
            return None
        return start, min(end, file_size - 1)

    # Suffix range "-N": the last N bytes of the file.
    if not end_s or not end_s.isdigit():
        return None
    suffix_length = int(end_s)
    if suffix_length == 0:
        return None
    return max(0, file_size - suffix_length), file_size - 1


async def _file_iterator(
    file_path: str, start: int, length: int
) -> AsyncIterator[bytes]:
    """Yield `length` bytes from `start` in chunks without blocking the loop."""
    remaining = length
    file = await asyncio.to_thread(open, file_path, "rb")
    try:
        if start:
            await asyncio.to_thread(file.seek, start)
        while remaining > 0:
            chunk = await asyncio.to_thread(file.read, min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        await asyncio.to_thread(file.close)


def _guess_content_type(file_path: str) -> str:
    mime, _ = mimetypes.guess_type(file_path)
    return mime or "application/octet-stream"


def _compute_etag(stat: os.stat_result) -> str:
    raw = f"{stat.st_mtime_ns}:{stat.st_size}"
    return f'"{hashlib.md5(raw.encode()).hexdigest()}"'
