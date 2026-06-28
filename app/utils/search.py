"""Helpers for the case-insensitive LIKE search used by the listing endpoints."""


def escape_like(value: str) -> str:
    """Escape LIKE wildcards so user input is matched literally.

    Without this a user typing ``%`` or ``_`` would inject wildcards. Pair the
    result with an explicit escape char, e.g.::

        col.ilike(f"%{escape_like(q)}%", escape="\\\\")
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
