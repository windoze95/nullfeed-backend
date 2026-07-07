"""Classify why a video can't be played or downloaded.

YouTube refuses some videos for reasons that are properties of the *video*, not
of our infrastructure: age restriction, channel-membership ("members only")
gating, paid/Premium content, private/removed videos, region locks, DRM, and
not-yet-premiered uploads. yt-dlp surfaces each as an error string (and, on
successful metadata extraction, as an ``availability`` value). This module maps
those onto a small canonical vocabulary stored in ``videos.unplayable_reason``
and exposed through the API so clients can label the video instead of failing
with a generic error.

Classification is deliberately conservative: anything transient (bot checks,
captchas, rate limits, network trouble) or simply unrecognized returns ``None``
so a flaky attempt never brands a playable video. A stored reason is cleared
whenever a download, preview, or instant-stream resolve later succeeds — so a
stale label (e.g. age restriction once cookies are configured, or a premiere
that has since gone live) heals itself on the next successful attempt.
"""

# Canonical reason values (also the client-facing API vocabulary).
AGE_RESTRICTED = "age_restricted"
MEMBERS_ONLY = "members_only"
PREMIUM = "premium"
PRIVATE = "private"
GEO_BLOCKED = "geo_blocked"
REMOVED = "removed"
DRM = "drm"
UPCOMING = "upcoming"
UNAVAILABLE = "unavailable"

# Reasons the server may still overcome, so automatic work (auto-download,
# preview pre-warm) should keep trying: an age gate passes once working YouTube
# cookies are configured (the poll's anonymous extraction can't know that), and
# an upcoming premiere becomes downloadable the moment it airs. Everything else
# is a hard wall no amount of retrying gets past.
SOFT_REASONS = frozenset({AGE_RESTRICTED, UPCOMING})

# Failures that must never label a video: they describe the session/network at
# that moment, not the video. Checked before the reason markers because a
# couple of them ("try again later", "not a bot") share words with permanent
# messages.
_TRANSIENT_MARKERS = (
    "not a bot",
    "captcha",
    "try again later",
    "rate limit",
    "rate-limit",
    "timed out",
    "timeout",
    "connection reset",
    "temporary failure",
    "unable to download webpage",
    "unable to download api page",
    "http error 5",
)

# Ordered (reason, markers) pairs matched against a lowercased yt-dlp error.
# Order matters where messages overlap: "Video unavailable. This video has been
# removed…" must classify as REMOVED, so REMOVED precedes UNAVAILABLE; the
# private-video message contains "sign in" but is matched by "private video"
# before any age marker could see it.
_REASON_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (MEMBERS_ONLY, ("members-only", "join this channel", "channel's members")),
    (PREMIUM, ("requires payment", "premium member")),
    (
        AGE_RESTRICTED,
        (
            "confirm your age",
            "age-restricted",
            "age restricted",
            "inappropriate for some users",
            "age_verification_required",
            "age_check_required",
        ),
    ),
    (PRIVATE, ("private video", "video is private")),
    (
        GEO_BLOCKED,
        (
            "not made this video available in your country",
            "not available in your country",
            "geo restricted",
            "geo-restricted",
        ),
    ),
    (DRM, ("drm protected", "drm-protected")),
    (UPCOMING, ("premieres in", "this live event will begin")),
    (
        REMOVED,
        (
            "has been removed",
            "been terminated",
            "copyright claim",
            "copyright grounds",
        ),
    ),
    (UNAVAILABLE, ("video unavailable", "no longer available")),
)

# yt-dlp ``availability`` values (from successful extraction, full or
# flat-playlist badges) that imply the video can't be fetched. ``needs_auth``
# is deliberately absent: it marks age-restricted videos whose extraction
# *succeeded*, i.e. the server is authorized and the video is playable.
_AVAILABILITY_REASONS = {
    "subscriber_only": MEMBERS_ONLY,
    "premium_only": PREMIUM,
    "private": PRIVATE,
}


class UnplayableVideoError(Exception):
    """A download/preview attempt failed for a reason inherent to the video.

    Deliberately not a ``RuntimeError``: the Celery download tasks auto-retry
    RuntimeErrors, and retrying a members-only or removed video is pointless.
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def extract_error_text(output: str) -> str:
    """The most error-relevant line of a yt-dlp output blob.

    yt-dlp prints its failure as an ``ERROR:`` line that is usually — but not
    always — last (aria2c chatter and diagnostics can follow). Prefer the last
    ERROR line; fall back to the last non-empty line.
    """
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    for line in reversed(lines):
        if "error" in line.lower():
            return line
    return lines[-1] if lines else ""


def classify_extraction_error(message: str | None) -> str | None:
    """Map a yt-dlp error message to a canonical reason, or None.

    None means "don't label": the error is transient, infrastructural, or
    unrecognized.
    """
    if not message:
        return None
    lowered = message.lower()
    if any(marker in lowered for marker in _TRANSIENT_MARKERS):
        return None
    for reason, markers in _REASON_MARKERS:
        if any(marker in lowered for marker in markers):
            return reason
    return None


def classify_availability(availability: str | None) -> str | None:
    """Map yt-dlp's ``availability`` field to a canonical reason, or None."""
    if not availability:
        return None
    return _AVAILABILITY_REASONS.get(availability)


def classify_live_status(live_status: str | None) -> str | None:
    """Map yt-dlp's ``live_status`` to a canonical reason, or None.

    Only ``is_upcoming`` (scheduled premieres / streams) is unplayable; a live
    or finished stream is left unlabeled.
    """
    return UPCOMING if live_status == "is_upcoming" else None


def classify_entry(entry: dict) -> str | None:
    """Reason for a yt-dlp metadata entry (full or flat), or None."""
    return classify_availability(entry.get("availability")) or classify_live_status(
        entry.get("live_status")
    )
