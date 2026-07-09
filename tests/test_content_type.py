"""Content-type classification: assign a stable kind-of-media label (regular,
short, live, premiere, age_restricted, members_only, premium) from yt-dlp
metadata, with access walls (members/premium/age) outranking format."""

from app.utils.content_type import (
    AGE_RESTRICTED,
    LIVE,
    MEMBERS_ONLY,
    PREMIERE,
    PREMIUM,
    REGULAR,
    SHORT,
    classify_content_type,
    content_type_for_reason,
    effective_hidden_content_types,
)


def test_plain_upload_is_regular():
    assert classify_content_type({"duration": 600}) == REGULAR
    assert classify_content_type({}) == REGULAR


def test_members_only_from_availability():
    assert classify_content_type({"availability": "subscriber_only"}) == MEMBERS_ONLY


def test_premium_from_availability():
    assert classify_content_type({"availability": "premium_only"}) == PREMIUM


def test_age_restricted_from_age_limit():
    assert classify_content_type({"age_limit": 18}) == AGE_RESTRICTED
    assert classify_content_type({"age_limit": 21}) == AGE_RESTRICTED


def test_age_restricted_from_needs_auth():
    # unplayable.py treats needs_auth as playable (None); content_type still flags
    # it as the age-restricted kind the viewer wants to spot.
    assert classify_content_type({"availability": "needs_auth"}) == AGE_RESTRICTED


def test_under_18_age_limit_is_not_age_restricted():
    assert classify_content_type({"age_limit": 0, "duration": 600}) == REGULAR


def test_premiere_from_upcoming():
    assert classify_content_type({"live_status": "is_upcoming"}) == PREMIERE


def test_live_from_live_statuses():
    for status in ("is_live", "post_live", "was_live"):
        assert classify_content_type({"live_status": status}) == LIVE


def test_live_from_was_live_flag():
    assert classify_content_type({"was_live": True}) == LIVE


def test_short_from_shorts_url():
    assert (
        classify_content_type({"webpage_url": "https://www.youtube.com/shorts/abc123"})
        == SHORT
    )


def test_short_from_duration_and_portrait_aspect():
    assert classify_content_type({"duration": 45, "aspect_ratio": 0.56}) == SHORT


def test_short_duration_without_portrait_is_regular():
    # A sub-minute landscape clip is a normal short video, not a Short.
    assert classify_content_type({"duration": 45, "aspect_ratio": 1.78}) == REGULAR
    # Flat entries carry no aspect_ratio → can't be a Short by the fallback.
    assert classify_content_type({"duration": 45}) == REGULAR


def test_long_portrait_video_is_not_a_short():
    assert classify_content_type({"duration": 600, "aspect_ratio": 0.56}) == REGULAR


def test_access_wall_outranks_format():
    # A members-only Short files under members_only — the axis the viewer gates on.
    entry = {
        "availability": "subscriber_only",
        "webpage_url": "https://www.youtube.com/shorts/x",
        "duration": 30,
        "aspect_ratio": 0.56,
    }
    assert classify_content_type(entry) == MEMBERS_ONLY


def test_age_restriction_outranks_live():
    assert (
        classify_content_type({"age_limit": 18, "live_status": "was_live"})
        == AGE_RESTRICTED
    )


def test_content_type_for_reason_maps_known_reasons():
    assert content_type_for_reason("members_only") == MEMBERS_ONLY
    assert content_type_for_reason("premium") == PREMIUM
    assert content_type_for_reason("age_restricted") == AGE_RESTRICTED
    assert content_type_for_reason("upcoming") == PREMIERE


def test_content_type_for_reason_ignores_non_media_reasons():
    for reason in ("private", "removed", "geo_blocked", "drm", "unavailable", None):
        assert content_type_for_reason(reason) is None


def test_effective_hidden_content_types_applies_default():
    # Unconfigured (NULL) → the default access walls (members-only + premium).
    assert effective_hidden_content_types(None) == [MEMBERS_ONLY, PREMIUM]
    # An explicit set is used as-is, including empty ("show everything").
    assert effective_hidden_content_types([]) == []
    assert effective_hidden_content_types([SHORT]) == [SHORT]
