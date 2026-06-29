"""Sponsor/ad-segment detection: SponsorBlock + AI fallback + endpoint (#88)."""

from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import select

import app.services.ad_segments as ad_segments
from app.config import settings
from app.database import async_session_factory
from app.models.video import Video
from tests.helpers import seed_channel, seed_video

pytestmark = pytest.mark.asyncio


def _fail(*_args, **_kwargs):
    raise AssertionError("should not be called")


# --- SponsorBlock ---------------------------------------------------------


async def test_fetch_sponsorblock_maps_segments(monkeypatch):
    rows = [
        {"segment": [10.0, 25.5], "category": "sponsor"},
        {"segment": [120.0, 140.0], "category": "selfpromo"},
        {"segment": [5.0], "category": "sponsor"},  # malformed -> skipped
        {"segment": [50.0, 50.0], "category": "sponsor"},  # zero-length -> skipped
    ]
    monkeypatch.setattr(
        ad_segments.httpx, "get", lambda *a, **k: httpx.Response(200, json=rows)
    )
    segs = ad_segments.fetch_sponsorblock_segments("yt-1")
    assert segs == [
        {"start": 10.0, "end": 25.5, "category": "sponsor"},
        {"start": 120.0, "end": 140.0, "category": "selfpromo"},
    ]


async def test_fetch_sponsorblock_404_returns_empty(monkeypatch):
    monkeypatch.setattr(ad_segments.httpx, "get", lambda *a, **k: httpx.Response(404))
    assert ad_segments.fetch_sponsorblock_segments("yt-2") == []


async def test_fetch_sponsorblock_error_returns_none(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(ad_segments.httpx, "get", boom)
    assert ad_segments.fetch_sponsorblock_segments("yt-3") is None


# --- resolve (SponsorBlock first, AI fallback) ----------------------------


async def test_resolve_prefers_sponsorblock(monkeypatch):
    segs = [{"start": 1.0, "end": 2.0, "category": "sponsor"}]
    monkeypatch.setattr(ad_segments, "fetch_sponsorblock_segments", lambda v: segs)
    monkeypatch.setattr(ad_segments, "detect_ad_segments_with_ai", _fail)
    assert ad_segments.resolve_ad_segments("yt") == segs


async def test_resolve_falls_back_to_ai(monkeypatch):
    monkeypatch.setattr(ad_segments, "fetch_sponsorblock_segments", lambda v: [])
    ai_segs = [{"start": 3.0, "end": 4.0, "category": "sponsor"}]
    monkeypatch.setattr(ad_segments, "detect_ad_segments_with_ai", lambda v: ai_segs)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
    assert ad_segments.resolve_ad_segments("yt") == ai_segs


async def test_resolve_no_ai_key_returns_empty(monkeypatch):
    monkeypatch.setattr(ad_segments, "fetch_sponsorblock_segments", lambda v: [])
    monkeypatch.setattr(ad_segments, "detect_ad_segments_with_ai", _fail)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    assert ad_segments.resolve_ad_segments("yt") == []


# --- AI detection ---------------------------------------------------------


async def test_ai_detection_parses_segments(monkeypatch):
    monkeypatch.setattr(
        ad_segments,
        "fetch_transcript",
        lambda v: [
            {"start": 0.0, "text": "intro"},
            {"start": 30.0, "text": "sponsor read"},
        ],
    )
    monkeypatch.setattr(
        ad_segments,
        "_claude_complete",
        lambda p: '```json\n[{"start": 30, "end": 60, "category": "sponsor"}]\n```',
    )
    assert ad_segments.detect_ad_segments_with_ai("yt") == [
        {"start": 30.0, "end": 60.0, "category": "sponsor"}
    ]


async def test_ai_detection_no_transcript(monkeypatch):
    monkeypatch.setattr(ad_segments, "fetch_transcript", lambda v: None)
    monkeypatch.setattr(ad_segments, "_claude_complete", _fail)
    assert ad_segments.detect_ad_segments_with_ai("yt") == []


# --- endpoint -------------------------------------------------------------


@pytest.fixture
def detect_delay(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr("app.api.videos.detect_ad_segments_task.delay", mock)
    return mock


async def test_ad_segments_requires_auth(client):
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel)
    resp = await client.get(f"/api/videos/{video.id}/ad-segments")
    assert resp.status_code == 401


async def test_ad_segments_unknown_video_404(client, make_user):
    _, headers = await make_user()
    resp = await client.get("/api/videos/nope/ad-segments", headers=headers)
    assert resp.status_code == 404


async def test_ad_segments_first_call_enqueues_pending(client, make_user, detect_delay):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel)

    resp = await client.get(f"/api/videos/{video.id}/ad-segments", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "PENDING", "segments": []}
    detect_delay.assert_called_once_with(video.id)

    async with async_session_factory() as db:
        v = (await db.execute(select(Video).where(Video.id == video.id))).scalar_one()
        assert v.ad_segments_status == "PENDING"


async def test_ad_segments_ready_returns_segments(client, make_user, detect_delay):
    user, headers = await make_user()
    segs = [{"start": 10.0, "end": 20.0, "category": "sponsor"}]
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel)
        v = (await db.execute(select(Video).where(Video.id == video.id))).scalar_one()
        v.ad_segments_status = "READY"
        v.ad_segments = segs
        await db.commit()

    resp = await client.get(f"/api/videos/{video.id}/ad-segments", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "READY", "segments": segs}
    detect_delay.assert_not_called()
