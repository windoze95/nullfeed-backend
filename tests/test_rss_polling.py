"""Tests for the RSS conditional-GET discovery path in download_manager."""

from unittest.mock import MagicMock

import app.services.download_manager as dm
from app.services.download_manager import (
    _parse_rss_entries,
    fetch_channel_rss,
    fetch_videos_metadata,
)

SAMPLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
  <yt:channelId>UCabc123</yt:channelId>
  <title>Test Channel</title>
  <entry>
    <id>yt:video:NEWEST00001</id>
    <yt:videoId>NEWEST00001</yt:videoId>
    <title>Newest Video</title>
    <published>2026-06-27T12:00:00+00:00</published>
  </entry>
  <entry>
    <id>yt:video:OLDER000002</id>
    <yt:videoId>OLDER000002</yt:videoId>
    <title>Older Video</title>
    <published>2026-06-26T12:00:00+00:00</published>
  </entry>
</feed>
"""


class _FakeResponse:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def test_parse_rss_entries_extracts_ids_titles_in_order():
    entries = _parse_rss_entries(SAMPLE_FEED)
    assert [e["youtube_video_id"] for e in entries] == ["NEWEST00001", "OLDER000002"]
    assert entries[0]["title"] == "Newest Video"
    assert entries[0]["published"] == "2026-06-27T12:00:00+00:00"


def test_parse_rss_entries_skips_entries_without_video_id():
    feed = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns="http://www.w3.org/2005/Atom">
  <entry><title>No id here</title></entry>
  <entry>
    <yt:videoId>HASID000001</yt:videoId>
    <title>Has id</title>
  </entry>
</feed>
"""
    entries = _parse_rss_entries(feed)
    assert [e["youtube_video_id"] for e in entries] == ["HASID000001"]


def test_fetch_channel_rss_non_uc_id_skips_network(monkeypatch):
    """Handles/usernames can't address the feed; must not hit the network."""
    called = MagicMock()
    monkeypatch.setattr(dm.httpx, "get", called)

    result = fetch_channel_rss("@somehandle")

    assert result == {"status": "unavailable"}
    called.assert_not_called()


def test_fetch_channel_rss_304_returns_not_modified(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResponse(status_code=304)

    monkeypatch.setattr(dm.httpx, "get", fake_get)

    result = fetch_channel_rss(
        "UCabc123", etag='W/"etag-1"', last_modified="Wed, 25 Jun 2026 00:00:00 GMT"
    )

    assert result == {"status": "not_modified"}
    # Conditional headers were sent from the stored validators.
    assert captured["headers"]["If-None-Match"] == 'W/"etag-1"'
    assert captured["headers"]["If-Modified-Since"] == "Wed, 25 Jun 2026 00:00:00 GMT"
    assert "channel_id=UCabc123" in captured["url"]


def test_fetch_channel_rss_200_returns_entries_and_validators(monkeypatch):
    def fake_get(url, headers=None, **kwargs):
        return _FakeResponse(
            status_code=200,
            text=SAMPLE_FEED,
            headers={
                "ETag": 'W/"etag-2"',
                "Last-Modified": "Fri, 27 Jun 2026 12:00:00 GMT",
            },
        )

    monkeypatch.setattr(dm.httpx, "get", fake_get)

    result = fetch_channel_rss("UCabc123")

    assert result["status"] == "ok"
    assert result["etag"] == 'W/"etag-2"'
    assert result["last_modified"] == "Fri, 27 Jun 2026 12:00:00 GMT"
    assert [e["youtube_video_id"] for e in result["entries"]] == [
        "NEWEST00001",
        "OLDER000002",
    ]


def test_fetch_channel_rss_no_conditional_headers_when_validators_absent(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, **kwargs):
        captured["headers"] = headers
        return _FakeResponse(status_code=200, text=SAMPLE_FEED)

    monkeypatch.setattr(dm.httpx, "get", fake_get)

    fetch_channel_rss("UCabc123")

    assert captured["headers"] == {}


def test_fetch_channel_rss_non_200_is_unavailable(monkeypatch):
    monkeypatch.setattr(dm.httpx, "get", lambda *a, **k: _FakeResponse(status_code=404))
    assert fetch_channel_rss("UCabc123") == {"status": "unavailable"}


def test_fetch_channel_rss_network_error_is_unavailable(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(dm.httpx, "get", boom)
    assert fetch_channel_rss("UCabc123") == {"status": "unavailable"}


def test_fetch_channel_rss_unparseable_body_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        dm.httpx,
        "get",
        lambda *a, **k: _FakeResponse(status_code=200, text="<<not xml>>"),
    )
    assert fetch_channel_rss("UCabc123") == {"status": "unavailable"}


def test_fetch_videos_metadata_preserves_order_and_omits_failures(monkeypatch):
    # yt-dlp emits one JSON line per resolved video; the second id failed.
    stdout = (
        '{"id": "AAA00000001", "title": "First", "duration": 100, '
        '"upload_date": "20260627"}\n'
        '{"id": "CCC00000003", "title": "Third", "duration": 300, '
        '"upload_date": "20260625"}\n'
    )
    fake_proc = MagicMock(returncode=1, stdout=stdout, stderr="")
    monkeypatch.setattr(dm.subprocess, "run", lambda *a, **k: fake_proc)

    result = fetch_videos_metadata(["AAA00000001", "BBB00000002", "CCC00000003"])

    # Requested order preserved; the unresolved middle id is dropped.
    assert [v["youtube_video_id"] for v in result] == ["AAA00000001", "CCC00000003"]
    assert result[0]["title"] == "First"
    assert result[0]["duration_seconds"] == 100
    assert result[0]["upload_date"] == "20260627"


def test_fetch_videos_metadata_empty_input_returns_empty(monkeypatch):
    # Must not shell out at all for an empty id list.
    called = MagicMock()
    monkeypatch.setattr(dm.subprocess, "run", called)
    assert fetch_videos_metadata([]) == []
    called.assert_not_called()
