"""Tests for channel_poller metadata refresh logic."""

from unittest.mock import MagicMock, patch

from app.models.channel import Channel
from app.services.channel_poller import _refresh_single_channel_metadata


def _make_channel(**overrides):
    ch = Channel(
        id="ch-1",
        youtube_channel_id="@testchannel",
        name="@testchannel",
        slug="testchannel",
    )
    for k, v in overrides.items():
        setattr(ch, k, v)
    return ch


@patch("app.services.channel_poller.fetch_channel_images")
@patch("app.services.channel_poller.fetch_channel_metadata")
def test_refresh_updates_name_and_images(mock_meta, mock_images):
    mock_meta.return_value = {
        "name": "Test Channel",
        "channel_id": "UCabc123",
        "handle": "@testchannel",
    }
    mock_images.return_value = {
        "avatar_url": "https://example.com/avatar.jpg",
        "banner_url": "https://example.com/banner.jpg",
    }
    db = MagicMock()
    # No duplicate channel found
    db.execute.return_value.scalar_one_or_none.return_value = None

    channel = _make_channel()
    _refresh_single_channel_metadata(channel, db)

    assert channel.name == "Test Channel"
    assert channel.youtube_channel_id == "UCabc123"
    assert channel.avatar_url == "https://example.com/avatar.jpg"
    assert channel.banner_url == "https://example.com/banner.jpg"
    assert channel.metadata_refreshed_at is not None
    db.commit.assert_called_once()


@patch("app.services.channel_poller.fetch_channel_images")
@patch("app.services.channel_poller.fetch_channel_metadata")
def test_refresh_skips_canonicalization_on_duplicate(mock_meta, mock_images):
    mock_meta.return_value = {
        "name": "Test Channel",
        "channel_id": "UCabc123",
        "handle": "@testchannel",
    }
    mock_images.return_value = {"avatar_url": None, "banner_url": None}
    db = MagicMock()
    # Simulate a duplicate channel owning this UC ID
    db.execute.return_value.scalar_one_or_none.return_value = _make_channel(
        id="ch-other", youtube_channel_id="UCabc123"
    )

    channel = _make_channel()
    _refresh_single_channel_metadata(channel, db)

    # Should NOT canonicalize since another channel owns the UC ID
    assert channel.youtube_channel_id == "@testchannel"


@patch("app.services.channel_poller.fetch_channel_images")
@patch("app.services.channel_poller.fetch_channel_metadata")
def test_refresh_keeps_existing_name(mock_meta, mock_images):
    mock_meta.return_value = {
        "name": "Resolved Name",
        "channel_id": "UCabc123",
        "handle": "@testchannel",
    }
    mock_images.return_value = {"avatar_url": None, "banner_url": None}
    db = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = None

    # Channel already has a custom display name
    channel = _make_channel(name="My Custom Name")
    _refresh_single_channel_metadata(channel, db)

    # Name should NOT be overwritten
    assert channel.name == "My Custom Name"


def test_cataloged_batch_preserves_feed_order(monkeypatch):
    """Within one poll, newer feed entries get later created_at stamps."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from app.models import Base
    from app.models.video import Video
    from app.services import channel_poller
    from app.utils.time import utcnow_naive

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    channel = _make_channel(last_checked_at=utcnow_naive())
    db.add(channel)
    db.commit()

    feed = {
        "videos": [
            {"youtube_video_id": "newest01234", "title": "Newest"},
            {"youtube_video_id": "middle01234", "title": "Middle"},
            {"youtube_video_id": "oldest01234", "title": "Oldest"},
        ]
    }
    monkeypatch.setattr(channel_poller, "fetch_channel_videos", lambda _: feed)

    channel_poller.poll_single_channel(channel.id, db)

    rows = db.execute(select(Video)).scalars().all()
    by_title = {v.title: v.created_at for v in rows}
    assert by_title["Newest"] > by_title["Middle"] > by_title["Oldest"]
    db.close()


def _poller_db_with_subscribers(
    *user_ids, initial_poll_done, youtube_channel_id="@testchannel"
):
    """Build an in-memory poller DB with a channel + subscribers.

    ``initial_poll_done`` sets last_checked_at so the poller treats the next
    poll's new rows as genuinely new (True) vs back-catalog ingestion (False).
    ``youtube_channel_id`` defaults to a handle (RSS unavailable -> yt-dlp
    fallback); pass a ``UC...`` id to exercise the RSS path.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models import Base
    from app.models.subscription import UserSubscription
    from app.utils.time import utcnow_naive

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    channel = _make_channel(
        youtube_channel_id=youtube_channel_id,
        last_checked_at=utcnow_naive() if initial_poll_done else None,
    )
    db.add(channel)
    for uid in user_ids:
        db.add(UserSubscription(user_id=uid, channel_id=channel.id))
    db.commit()
    return db, channel


def test_new_episode_event_emitted_per_subscriber(monkeypatch):
    from app.services import channel_poller

    db, channel = _poller_db_with_subscribers("u1", "u2", initial_poll_done=True)
    feed = {"videos": [{"youtube_video_id": "vid00000001", "title": "New One"}]}
    monkeypatch.setattr(channel_poller, "fetch_channel_videos", lambda _: feed)
    publish_mock = MagicMock()
    monkeypatch.setattr(channel_poller, "publish_new_episode", publish_mock)

    channel_poller.poll_single_channel(channel.id, db)

    # One event per (subscriber x new video): 2 subscribers, 1 new video.
    assert publish_mock.call_count == 2
    assert {call.args[1] for call in publish_mock.call_args_list} == {"u1", "u2"}
    for call in publish_mock.call_args_list:
        assert call.kwargs["channel_id"] == channel.id
        assert call.kwargs["title"] == "New One"
        assert call.kwargs["youtube_video_id"] == "vid00000001"
    db.close()


def test_new_episode_event_suppressed_on_initial_poll(monkeypatch):
    from app.services import channel_poller

    db, channel = _poller_db_with_subscribers("u1", initial_poll_done=False)
    feed = {"videos": [{"youtube_video_id": "vid00000001", "title": "Back Catalog"}]}
    monkeypatch.setattr(channel_poller, "fetch_channel_videos", lambda _: feed)
    publish_mock = MagicMock()
    monkeypatch.setattr(channel_poller, "publish_new_episode", publish_mock)

    channel_poller.poll_single_channel(channel.id, db)

    publish_mock.assert_not_called()
    db.close()


def test_new_episode_event_skipped_for_already_known_video(monkeypatch):
    """A second poll that surfaces no new rows emits nothing."""
    from app.services import channel_poller

    db, channel = _poller_db_with_subscribers("u1", initial_poll_done=True)
    feed = {"videos": [{"youtube_video_id": "vid00000001", "title": "New One"}]}
    monkeypatch.setattr(channel_poller, "fetch_channel_videos", lambda _: feed)
    publish_mock = MagicMock()
    monkeypatch.setattr(channel_poller, "publish_new_episode", publish_mock)

    channel_poller.poll_single_channel(channel.id, db)  # first sighting: 1 event
    channel_poller.poll_single_channel(channel.id, db)  # already known: 0 events

    assert publish_mock.call_count == 1
    db.close()


# --- RSS routine-discovery path -------------------------------------------

_UC_ID = "UCabc1230000000000000000"


def test_initial_poll_uses_ytdlp_not_rss(monkeypatch):
    """A never-cataloged channel must use the full yt-dlp back-catalog path."""
    from app.services import channel_poller

    db, channel = _poller_db_with_subscribers(
        "u1", initial_poll_done=False, youtube_channel_id=_UC_ID
    )
    rss_mock = MagicMock()
    monkeypatch.setattr(channel_poller, "fetch_channel_rss", rss_mock)
    feed = {"videos": [{"youtube_video_id": "BACK0000001", "title": "Back catalog"}]}
    monkeypatch.setattr(channel_poller, "fetch_channel_videos", lambda _: feed)

    result = channel_poller.poll_single_channel(channel.id, db)

    rss_mock.assert_not_called()
    assert len(result["cataloged_ids"]) == 1
    db.close()


def test_routine_poll_304_short_circuits(monkeypatch):
    """A 304 does no yt-dlp work at all and catalogs nothing."""
    from app.services import channel_poller

    db, channel = _poller_db_with_subscribers(
        "u1", initial_poll_done=True, youtube_channel_id=_UC_ID
    )
    monkeypatch.setattr(
        channel_poller, "fetch_channel_rss", lambda *a, **k: {"status": "not_modified"}
    )
    videos_mock = MagicMock()
    meta_mock = MagicMock()
    monkeypatch.setattr(channel_poller, "fetch_channel_videos", videos_mock)
    monkeypatch.setattr(channel_poller, "fetch_videos_metadata", meta_mock)

    result = channel_poller.poll_single_channel(channel.id, db)

    assert result == {"cataloged_ids": [], "auto_download_ids": []}
    videos_mock.assert_not_called()
    meta_mock.assert_not_called()
    assert channel.last_checked_at is not None
    db.close()


def test_routine_poll_sends_stored_validators(monkeypatch):
    """Persisted ETag/Last-Modified are sent as conditional headers next poll."""
    from app.services import channel_poller

    db, channel = _poller_db_with_subscribers(
        "u1", initial_poll_done=True, youtube_channel_id=_UC_ID
    )
    channel.rss_etag = 'W/"stored"'
    channel.rss_last_modified = "Wed, 25 Jun 2026 00:00:00 GMT"
    db.commit()

    captured = {}

    def fake_rss(channel_id, etag=None, last_modified=None):
        captured["channel_id"] = channel_id
        captured["etag"] = etag
        captured["last_modified"] = last_modified
        return {"status": "not_modified"}

    monkeypatch.setattr(channel_poller, "fetch_channel_rss", fake_rss)

    channel_poller.poll_single_channel(channel.id, db)

    assert captured["channel_id"] == _UC_ID
    assert captured["etag"] == 'W/"stored"'
    assert captured["last_modified"] == "Wed, 25 Jun 2026 00:00:00 GMT"
    db.close()


def test_routine_poll_rss_new_id_catalogs_via_metadata(monkeypatch):
    """New feed IDs fall through to yt-dlp metadata only for those IDs."""
    from sqlalchemy import select

    from app.models.video import Video
    from app.services import channel_poller

    db, channel = _poller_db_with_subscribers(
        "u1", initial_poll_done=True, youtube_channel_id=_UC_ID
    )
    monkeypatch.setattr(
        channel_poller,
        "fetch_channel_rss",
        lambda *a, **k: {
            "status": "ok",
            "entries": [
                {"youtube_video_id": "NEW00000001"},
                {"youtube_video_id": "NEW00000002"},
            ],
            "etag": 'W/"fresh"',
            "last_modified": "Fri, 27 Jun 2026 12:00:00 GMT",
        },
    )
    meta_calls = {}

    def fake_meta(ids):
        meta_calls["ids"] = ids
        return [
            {
                "youtube_video_id": vid,
                "title": vid,
                "duration_seconds": 0,
                "upload_date": None,
            }
            for vid in ids
        ]

    monkeypatch.setattr(channel_poller, "fetch_videos_metadata", fake_meta)
    videos_mock = MagicMock()
    monkeypatch.setattr(channel_poller, "fetch_channel_videos", videos_mock)
    publish_mock = MagicMock()
    monkeypatch.setattr(channel_poller, "publish_new_episode", publish_mock)

    result = channel_poller.poll_single_channel(channel.id, db)

    # Only the new IDs were sent to yt-dlp; the full-listing path was untouched.
    assert meta_calls["ids"] == ["NEW00000001", "NEW00000002"]
    videos_mock.assert_not_called()
    assert len(result["cataloged_ids"]) == 2
    # Validators persisted for the next conditional GET.
    db.refresh(channel)
    assert channel.rss_etag == 'W/"fresh"'
    assert channel.rss_last_modified == "Fri, 27 Jun 2026 12:00:00 GMT"
    # new_episode emitted for each new video (1 subscriber x 2 videos).
    assert publish_mock.call_count == 2
    rows = db.execute(select(Video)).scalars().all()
    assert {r.youtube_video_id for r in rows} == {"NEW00000001", "NEW00000002"}
    db.close()


def test_routine_poll_rss_all_known_persists_etag_without_metadata(monkeypatch):
    """A fresh feed whose IDs are all known catalogs nothing but updates ETag."""
    from app.models.video import Video
    from app.services import channel_poller

    db, channel = _poller_db_with_subscribers(
        "u1", initial_poll_done=True, youtube_channel_id=_UC_ID
    )
    db.add(
        Video(
            id="v-known",
            youtube_video_id="KNOWN000001",
            channel_id=channel.id,
            title="Known",
            status="CATALOGED",
        )
    )
    db.commit()

    monkeypatch.setattr(
        channel_poller,
        "fetch_channel_rss",
        lambda *a, **k: {
            "status": "ok",
            "entries": [{"youtube_video_id": "KNOWN000001"}],
            "etag": 'W/"known-etag"',
            "last_modified": None,
        },
    )
    meta_mock = MagicMock()
    monkeypatch.setattr(channel_poller, "fetch_videos_metadata", meta_mock)
    monkeypatch.setattr(channel_poller, "fetch_channel_videos", MagicMock())

    result = channel_poller.poll_single_channel(channel.id, db)

    assert result == {"cataloged_ids": [], "auto_download_ids": []}
    meta_mock.assert_not_called()
    db.refresh(channel)
    assert channel.rss_etag == 'W/"known-etag"'
    db.close()


def test_routine_poll_falls_back_to_ytdlp_when_rss_unavailable(monkeypatch):
    """A handle-id channel can't use RSS, so discovery falls back to yt-dlp."""
    from app.services import channel_poller

    # Default handle id -> the real fetch_channel_rss reports unavailable with
    # no network call, so discovery uses the (mocked) full listing.
    db, channel = _poller_db_with_subscribers("u1", initial_poll_done=True)
    feed = {"videos": [{"youtube_video_id": "FALLBACK001", "title": "Fallback"}]}
    videos_mock = MagicMock(return_value=feed)
    monkeypatch.setattr(channel_poller, "fetch_channel_videos", videos_mock)

    result = channel_poller.poll_single_channel(channel.id, db)

    videos_mock.assert_called_once()
    assert len(result["cataloged_ids"]) == 1
    db.close()
