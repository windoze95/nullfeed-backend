"""Classify a video's content TYPE — a stable "what kind of media is this" label,
distinct from ``videos.unplayable_reason``.

``unplayable_reason`` answers "why can't I play this" and *self-heals* to NULL the
moment a video becomes playable — useless as a durable kind-of-media tag. This
module produces ``videos.content_type``: assigned at catalog time from yt-dlp
metadata and never cleared, so clients can badge it and gate it per channel
(Shorts/clips, livestreams, members-only, age-restricted, …).

One value per video, highest-precedence wins. An access wall (members / premium /
age) outranks a format (premiere / live / short), because that's the axis a
viewer gates on — a members-only Short is filed as members-only. Plain uploads,
and anything we can't tell apart, are ``regular``.

Signal availability differs by extraction depth: flat ``--flat-playlist`` entries
(the back-catalog scan) carry ``availability``/``live_status`` from the tab
badges but not ``age_limit``/``aspect_ratio``; full ``--dump-json`` entries (the
routine per-video poll) carry all of them. Shorts are only reliably known from
the dedicated /shorts tab scan (which marks entries), so the duration heuristic
here is a conservative fallback.
"""

# Canonical content_type values (also the client-facing API vocabulary).
REGULAR = "regular"
SHORT = "short"
LIVE = "live"
PREMIERE = "premiere"
AGE_RESTRICTED = "age_restricted"
MEMBERS_ONLY = "members_only"
PREMIUM = "premium"

# Types cataloged for visibility but never auto-downloaded or announced as new
# episodes by default: auto-pulling every discovered Short and multi-hour
# livestream is exactly what would flood storage, so they stay hands-off until a
# per-channel gate opts them in. Access-walled types already sit out downloads
# via unplayable_reason, and premieres via their SOFT upcoming reason.
CATALOG_ONLY_TYPES = frozenset({SHORT, LIVE})

# Longest a video can be and still count as a Short by the duration fallback
# (YouTube caps Shorts at 60s; a second of slack for rounding). Only consulted
# when nothing already marked it a Short.
_SHORT_MAX_SECONDS = 61


def classify_content_type(entry: dict) -> str:
    """The content_type for a yt-dlp metadata entry (flat or full). Never None —
    an unrecognized/plain upload is ``regular``."""
    # Access walls first — they outrank format.
    availability = entry.get("availability")
    if availability == "subscriber_only":
        return MEMBERS_ONLY
    if availability == "premium_only":
        return PREMIUM
    # An explicit 18+ age_limit, or availability marking an age wall that
    # extraction still got past (``needs_auth``). Note unplayable.py treats
    # needs_auth as *playable* (None) — but it's exactly the age-restricted kind
    # the viewer wants to spot, so content_type diverges here on purpose.
    if (entry.get("age_limit") or 0) >= 18 or availability == "needs_auth":
        return AGE_RESTRICTED

    # Format.
    live_status = entry.get("live_status")
    if live_status == "is_upcoming":
        return PREMIERE
    if (
        live_status in ("is_live", "post_live", "was_live")
        or entry.get("was_live") is True
    ):
        return LIVE

    if _is_short(entry):
        return SHORT
    return REGULAR


def _is_short(entry: dict) -> bool:
    """A Short/clip: authoritatively a ``/shorts/`` URL (the /shorts tab scan sets
    these), else a short *and portrait* upload. Duration alone is too loose —
    plenty of regular uploads run under a minute — so the fallback also requires
    a vertical aspect, which only full extraction provides."""
    url = entry.get("webpage_url") or entry.get("url") or ""
    if "/shorts/" in url:
        return True
    duration = entry.get("duration") or 0
    if not 0 < duration <= _SHORT_MAX_SECONDS:
        return False
    # aspect_ratio = width / height; Shorts are portrait (< 1). Trust it only
    # when yt-dlp provided it (absent on flat entries → not a Short by this path).
    aspect = entry.get("aspect_ratio")
    return aspect is not None and aspect < 1.0


# An unplayable_reason implies a content_type for stub rows built from a
# classified extraction *failure*, where there's no full metadata to classify
# from. Reasons with no kind-of-media meaning (private/removed/geo/drm/
# unavailable) map to nothing and leave content_type NULL (→ treated as regular).
_REASON_TO_CONTENT_TYPE = {
    "members_only": MEMBERS_ONLY,
    "premium": PREMIUM,
    "age_restricted": AGE_RESTRICTED,
    "upcoming": PREMIERE,
}


def content_type_for_reason(reason: str | None) -> str | None:
    """The content_type implied by an ``unplayable_reason``, or None."""
    return _REASON_TO_CONTENT_TYPE.get(reason or "")
