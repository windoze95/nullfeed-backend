"""Time helpers shared across the app."""

from datetime import datetime, timezone


def utcnow_naive() -> datetime:
    """Return the current UTC time as a naive datetime.

    SQLite stores datetimes without timezone info, so the codebase
    standardizes on naive UTC datetimes to avoid ever comparing
    timezone-aware and naive values.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
