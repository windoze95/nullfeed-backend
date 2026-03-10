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

    # Parse range header: "bytes
