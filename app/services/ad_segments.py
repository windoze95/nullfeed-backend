"""Detect sponsor/ad segments in a video for client-side skipping (#88).

Two sources, tried in order:

1. **SponsorBlock** — the free community API. Accurate and instant for popular
   videos, and the only cost is one HTTP GET.
2. **AI fallback** — only when SponsorBlock has no data *and* an Anthropic key
   is configured: read the video's transcript and ask Claude to mark sponsor
   reads. Covers the long tail SponsorBlock hasn't catalogued.

Segments are ``{start, end, category}`` in seconds; clients seek past them during
playback. This module is pure (no DB); the Celery task stores the result.
"""

import json
import logging

import httpx

from app.config import settings
from app.services.download_manager import fetch_transcript

logger = logging.getLogger(__name__)

SPONSORBLOCK_API_URL = "https://sponsor.ajay.app/api/skipSegments"
# Only paid-promotion categories — we cut sponsor reads and self-promo, not the
# intro/outro/interaction categories SponsorBlock also tracks.
SPONSORBLOCK_CATEGORIES = ("sponsor", "selfpromo")
_REQUEST_TIMEOUT = 15

_AI_MODEL = "claude-sonnet-4-6"  # matches the recommendation engine
_AI_MAX_TOKENS = 1024  # a segment list is small

_AI_PROMPT = """You are given a timestamped transcript of a YouTube video. \
Identify the time ranges that are paid sponsor reads or self-promotion (e.g. \
"this video is sponsored by", promo codes, "check out my merch/Patreon/Nord VPN"). \
Do NOT mark the actual content, or intros/outros that aren't promotional.

Return ONLY a JSON array (no prose, no markdown) where each element is:
{{"start": <seconds, number>, "end": <seconds, number>, "category": "sponsor"}}
Return [] if there are no sponsor segments.

Transcript (start_seconds: text):
{transcript}
"""


def fetch_sponsorblock_segments(youtube_video_id: str) -> list[dict] | None:
    """Query SponsorBlock for sponsor segments.

    Returns a list of ``{start, end, category}``; ``[]`` when SponsorBlock has no
    data for the video (HTTP 404); or ``None`` on a transport/HTTP error, so the
    caller can distinguish "no data" from "couldn't check" and fall back to AI in
    both cases without treating an error as a definitive "no ads".
    """
    params = [("videoID", youtube_video_id)]
    params += [("category", category) for category in SPONSORBLOCK_CATEGORIES]
    try:
        resp = httpx.get(SPONSORBLOCK_API_URL, params=params, timeout=_REQUEST_TIMEOUT)
    except httpx.HTTPError as exc:
        logger.warning("SponsorBlock request failed for %s: %s", youtube_video_id, exc)
        return None

    if resp.status_code == 404:
        return []  # no community-submitted segments for this video
    if resp.status_code != 200:
        logger.warning(
            "SponsorBlock returned HTTP %s for %s", resp.status_code, youtube_video_id
        )
        return None

    try:
        rows = resp.json()
    except ValueError:
        return None

    segments: list[dict] = []
    for row in rows:
        seg = row.get("segment")
        if isinstance(seg, list) and len(seg) == 2:
            try:
                start, end = float(seg[0]), float(seg[1])
            except (TypeError, ValueError):
                continue
            if end > start:
                segments.append(
                    {
                        "start": round(start, 2),
                        "end": round(end, 2),
                        "category": row.get("category", "sponsor"),
                    }
                )
    return segments


def _claude_complete(prompt: str) -> str:
    """Run one synchronous Claude completion and return the response text."""
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    message = client.messages.create(
        model=_AI_MODEL,
        max_tokens=_AI_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    block = message.content[0]
    return block.text if hasattr(block, "text") else ""


def _parse_segments(text: str) -> list[dict]:
    """Parse Claude's JSON array of segments, tolerating markdown code fences."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("Expected a JSON array of segments")

    segments: list[dict] = []
    for item in data:
        try:
            start, end = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > start:
            segments.append(
                {
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "category": item.get("category", "sponsor"),
                }
            )
    return segments


def detect_ad_segments_with_ai(youtube_video_id: str) -> list[dict]:
    """AI fallback: read the transcript and ask Claude to mark sponsor reads.

    Returns ``[]`` when no transcript is available or detection fails.
    """
    cues = fetch_transcript(youtube_video_id)
    if not cues:
        return []
    transcript = "\n".join(f"{cue['start']}: {cue['text']}" for cue in cues)
    try:
        text = _claude_complete(_AI_PROMPT.format(transcript=transcript))
        return _parse_segments(text)
    except Exception as exc:
        logger.warning("AI ad detection failed for %s: %s", youtube_video_id, exc)
        return []


def resolve_ad_segments(youtube_video_id: str) -> list[dict]:
    """SponsorBlock first; fall back to AI-from-transcript only when SponsorBlock
    has nothing (no data or an error) *and* an Anthropic key is configured.

    Returns a possibly-empty list of ``{start, end, category}``.
    """
    sponsorblock = fetch_sponsorblock_segments(youtube_video_id)
    if sponsorblock:
        return sponsorblock
    if settings.anthropic_api_key:
        return detect_ad_segments_with_ai(youtube_video_id)
    return []
