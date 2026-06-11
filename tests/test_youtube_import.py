"""YouTube import service tests — yt-dlp subprocess is mocked (design 1.2)."""

import subprocess

import pytest

from app.services import youtube_import
from app.services.youtube_import import (
    YoutubeResolveError,
    YoutubeResolveTimeoutError,
    _normalize_handle,
    get_suggestions,
    resolve_handle,
)
from tests.helpers import IDENTITY_JSON, fake_completed_process

pytestmark = pytest.mark.asyncio


async def test_resolve_parses_identity_fields(monkeypatch):
    monkeypatch.setattr(
        youtube_import.subprocess,
        "run",
        lambda *a, **kw: fake_completed_process(IDENTITY_JSON),
    )
    result = await resolve_handle("@testchannel")
    assert result == {
        "handle": "@testchannel",
        "channel_id": "UCabc123",
        "name": "Test Channel",
        "description": "A test channel",
        "avatar_url": "https://img/avatar.jpg",
        "banner_url": "https://img/banner.jpg",
        "follower_count": 12345,
    }


async def test_resolve_avatar_falls_back_to_largest_squareish(monkeypatch):
    data = dict(IDENTITY_JSON)
    data["thumbnails"] = [
        {"url": "https://img/wide.jpg", "width": 1280, "height": 720},
        {"url": "https://img/small-square.jpg", "width": 88, "height": 88},
        {"url": "https://img/big-square.jpg", "width": 800, "height": 800},
    ]
    monkeypatch.setattr(
        youtube_import.subprocess,
        "run",
        lambda *a, **kw: fake_completed_process(data),
    )
    result = await resolve_handle("@fallback")
    assert result["avatar_url"] == "https://img/big-square.jpg"
    assert result["banner_url"] is None


async def test_resolve_caches_by_normalized_handle(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return fake_completed_process(IDENTITY_JSON)

    monkeypatch.setattr(youtube_import.subprocess, "run", fake_run)

    await resolve_handle("testchannel")
    await resolve_handle("@testchannel")
    await resolve_handle("https://www.youtube.com/@testchannel")
    assert len(calls) == 1


async def test_normalize_handle_variants():
    assert _normalize_handle("@name") == "@name"
    assert _normalize_handle("name") == "@name"
    assert _normalize_handle(" name ") == "@name"
    assert _normalize_handle("https://www.youtube.com/@name/videos") == "@name"
    assert _normalize_handle("https://www.youtube.com/c/SomeName") == "@SomeName"
    uc_id = "UC" + "a" * 22
    assert _normalize_handle(uc_id) == uc_id
    assert _normalize_handle(f"https://www.youtube.com/channel/{uc_id}") == uc_id
    with pytest.raises(YoutubeResolveError):
        _normalize_handle("https://www.youtube.com/watch?v=abc")


async def test_resolve_failure_maps_to_404(client, monkeypatch):
    monkeypatch.setattr(
        youtube_import.subprocess,
        "run",
        lambda *a, **kw: fake_completed_process(
            "", returncode=1, stderr="ERROR: Unable to download webpage"
        ),
    )
    resp = await client.post("/api/youtube/resolve", json={"handle": "@nope"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "YouTube channel not found"


async def test_resolve_timeout_maps_to_504(client, monkeypatch):
    def raise_timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

    monkeypatch.setattr(youtube_import.subprocess, "run", raise_timeout)

    # The timeout error is a YoutubeResolveError subclass (auth.py relies on it).
    with pytest.raises(YoutubeResolveTimeoutError):
        await resolve_handle("@slow")
    assert issubclass(YoutubeResolveTimeoutError, YoutubeResolveError)

    resp = await client.post("/api/youtube/resolve", json={"handle": "@slow"})
    assert resp.status_code == 504
    assert resp.json()["detail"] == "YouTube lookup timed out"


OWN_CHANNEL_ID = "UCown0000000000000000000"


def _suggestions_dispatcher():
    """Mock yt-dlp: no channels tab, two public playlists with rankable entries."""

    def fake_run(cmd, **kwargs):
        url = cmd[-1]
        if url.endswith("/channels"):
            return fake_completed_process(
                "",
                returncode=1,
                stderr="ERROR: This channel does not have a channels tab",
            )
        if url.endswith("/playlists"):
            return fake_completed_process(
                {
                    "entries": [
                        {"id": "PL1"},
                        {"url": "https://www.youtube.com/playlist?list=PL2"},
                    ]
                }
            )
        if "PL1" in url:
            entries = [
                {"channel_id": "UCaaa", "channel": "Alpha", "uploader_id": "@alpha"},
                {"channel_id": "UCaaa", "channel": "Alpha", "uploader_id": "@alpha"},
                {"channel_id": "UCaaa", "channel": "Alpha", "uploader_id": "@alpha"},
                {"channel_id": OWN_CHANNEL_ID, "channel": "Self"},
                {"channel_id": "UCbbb", "channel": "Beta"},
            ]
            return fake_completed_process({"entries": entries})
        if "PL2" in url:
            entries = [
                {"channel_id": "UCbbb", "channel": "Beta"},
                {"channel_id": "UCccc", "channel": "Gamma"},
                {"channel_id": OWN_CHANNEL_ID, "channel": "Self"},
            ]
            return fake_completed_process({"entries": entries})
        # Identity resolve for the user's own channel.
        return fake_completed_process(
            {"channel_id": OWN_CHANNEL_ID, "channel": "Self", "thumbnails": []}
        )

    return fake_run


async def test_suggestions_playlists_ranked_own_channel_excluded(monkeypatch):
    monkeypatch.setattr(youtube_import.subprocess, "run", _suggestions_dispatcher())

    suggestions = await get_suggestions("@self")

    assert [s["youtube_channel_id"] for s in suggestions] == [
        "UCaaa",
        "UCbbb",
        "UCccc",
    ]
    by_id = {s["youtube_channel_id"]: s for s in suggestions}
    assert by_id["UCaaa"]["score"] == 3
    assert by_id["UCbbb"]["score"] == 2
    assert by_id["UCccc"]["score"] == 1
    assert by_id["UCaaa"]["name"] == "Alpha"
    assert by_id["UCaaa"]["handle"] == "@alpha"
    assert all(s["source"] == "playlists" for s in suggestions)
    assert OWN_CHANNEL_ID not in by_id


async def test_suggestions_featured_channels_tab(monkeypatch):
    def fake_run(cmd, **kwargs):
        url = cmd[-1]
        if url.endswith("/channels"):
            return fake_completed_process(
                {
                    "entries": [
                        {
                            "channel_id": "UCfeat",
                            "channel": "Featured",
                            "uploader_id": "@feat",
                        }
                    ]
                }
            )
        if url.endswith("/playlists"):
            return fake_completed_process({"entries": []})
        return fake_completed_process(
            {"channel_id": OWN_CHANNEL_ID, "channel": "Self", "thumbnails": []}
        )

    monkeypatch.setattr(youtube_import.subprocess, "run", fake_run)

    suggestions = await get_suggestions("@self")
    assert suggestions == [
        {
            "youtube_channel_id": "UCfeat",
            "name": "Featured",
            "handle": "@feat",
            "avatar_url": None,
            "source": "featured",
            "score": 100,
        }
    ]


async def test_suggestions_endpoint_empty_is_normal(client, monkeypatch):
    def fake_run(cmd, **kwargs):
        url = cmd[-1]
        if url.endswith(("/channels", "/playlists")):
            return fake_completed_process(
                "", returncode=1, stderr="ERROR: tab not available"
            )
        return fake_completed_process(
            {"channel_id": OWN_CHANNEL_ID, "channel": "Self", "thumbnails": []}
        )

    monkeypatch.setattr(youtube_import.subprocess, "run", fake_run)

    resp = await client.post("/api/youtube/suggestions", json={"handle": "@self"})
    assert resp.status_code == 200
    assert resp.json() == {"suggestions": []}
