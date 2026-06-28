"""Opaque cursor helpers for keyset pagination.

Video listings order by ``(coalesce(uploaded_at, created_at) DESC, id DESC)``.
A cursor encodes the sort value and id of the last row on a page so the next
page can resume with a keyset predicate instead of an OFFSET (which degrades on
deep pages). Tokens are base64 of ``"<iso-datetime>|<id>"`` — opaque to clients,
trivially decodable here.
"""

import base64
from datetime import datetime


def encode_cursor(sort_value: datetime, item_id: str) -> str:
    """Encode the (sort_value, id) of the last row on a page into a token."""
    raw = f"{sort_value.isoformat()}|{item_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(token: str) -> tuple[datetime, str] | None:
    """Decode a cursor token, or return None if it is malformed.

    ``id`` is a UUID and the datetime is ISO-8601, so neither contains the ``|``
    separator; splitting once is unambiguous.
    """
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        sort_str, item_id = raw.split("|", 1)
        return datetime.fromisoformat(sort_str), item_id
    except (ValueError, TypeError):
        return None
