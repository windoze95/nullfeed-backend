"""Discover Shorts + livestreams (the /shorts and /streams tabs the main /videos
tab excludes), tag them by kind, and keep them catalog-only so they're visible
and gate-able but never flood auto-download."""

import json
import uuid
from unittest.mock import MagicMock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.models.channel import Channel
from app.models.video import Video
from app.services import channel_poller, download_manager
from app.services.channel_poller import _merge_tab_entries
from app.services.download_manager import fetch_channel_tab
from app.utils.content_type import LIVE, MEMBERS_ONLY, PREMIERE, REGULAR, SHORT
from tests.helpers import fake_completed_process


def test_fetch_channel_tab_forces_default_when_regular(monkeypatch):
    entries = [
        {"id": "AAA00000001", "title": "Clip", "duration": 30},
        {
            "id": "BBB00000002",
            "title": "Members clip",
            "duration": 20,
            "availability": "subscriber_only",
        },
    ]
    stdout = "\n".join(json.dumps(e) for e in entries)
    monkeypatch.setattr(
        download_manager.subprocess,
        "run",
        lambda *a, **k: fake_completed_process(stdout),
    )

    result = fetch_channel_tab("UCabc", "/shorts", SHORT)

    # A flat entry can't prove it's a Short (no aspect signal) → the /shorts tab
    # default applies; an access wall still wins.
    assert [v["content_type"] for v in result] == [SHORT, MEMBERS_ONLY]


def test_fetch_channel_tab_streams_default_live_but_keeps_premiere(monkeypatch):
    entries = [
        {"id": "AAA00000001", "title": "Past stream", "duration": 3600},
        {"id": "CCC00000003", "title": "Scheduled", "live_status": "is_upcoming"},
    ]
    stdout = "\n".join(json.dumps(e) for e in entries)
    monkeypatch.setattr(
        download_manager.subprocess,
        "run",
        lambda *a, **k: fake_completed_process(stdout),
    )

    result = fetch_channel_tab("UCabc", "/streams", LIVE)

    # Plain stream entries fall back to live; a scheduled premiere keeps premiere.
    assert [v["content_type"] for v in result] == [LIVE, PREMIERE]


def test_fetch_channel_tab_empty_on_error(monkeypatch):
    monkeypatch.setattr(
        download_manager.subprocess,
        "run",
        lambda *a, **k: fake_completed_process("", returncode=1),
    )
    assert fetch_channel_tab("UCabc", "/shorts", SHORT) == []


def test_merge_tab_entries_dedups_and_upgrades_type():
    videos = [
        {"youtube_video_id": "V1", "title": "Regular", "content_type": REGULAR},
        {"youtube_video_id": "S1", "title": "Past stream", "content_type": REGULAR},
    ]
    shorts = [{"youtube_video_id": "H1", "title": "Short", "content_type": SHORT}]
    streams = [{"youtube_video_id": "S1", "title": "Past stream", "content_type": LIVE}]

    merged = _merge_tab_entries(videos, shorts, streams)

    # Order follows the first list, new ids appended; the cross-listed S1 is kept
    # once (in its /videos position) and its type upgraded regular → live.
    assert [e["youtube_video_id"] for e in merged] == ["V1", "S1", "H1"]
    by_id = {e["youtube_video_id"]: e["content_type"] for e in merged}
    assert by_id == {"V1": REGULAR, "S1": LIVE, "H1": SHORT}


def _mem_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_catalog_gates_shorts_and_lives_from_autodownload(monkeypatch):
    db = _mem_session()
    channel = Channel(
        id=str(uuid.uuid4()),
        youtube_channel_id="UCabc1230000000000000000",
        name="Test",
        slug="test",
    )
    db.add(channel)
    db.commit()

    emit_mock = MagicMock()
    monkeypatch.setattr(channel_poller, "_emit_new_episode_events", emit_mock)
    auto_mock = MagicMock(return_value=[])
    monkeypatch.setattr(channel_poller, "_determine_auto_downloads", auto_mock)

    yt_videos = [
        {
            "youtube_video_id": "AAA00000001",
            "title": "Regular",
            "content_type": REGULAR,
        },
        {"youtube_video_id": "BBB00000002", "title": "Clip", "content_type": SHORT},
        {"youtube_video_id": "CCC00000003", "title": "Stream", "content_type": LIVE},
    ]
    result = channel_poller._catalog_videos(
        channel, yt_videos, db, had_initial_poll=True, update_schedule=False
    )

    # All three are cataloged and typed…
    assert len(result["cataloged_ids"]) == 3
    types = dict(db.execute(select(Video.youtube_video_id, Video.content_type)).all())
    assert types == {
        "AAA00000001": REGULAR,
        "BBB00000002": SHORT,
        "CCC00000003": LIVE,
    }

    # …but the Short and livestream are held out of auto-download candidates and
    # new-episode events; only the regular upload flows through.
    candidate_ytids = {
        db.get(Video, vid).youtube_video_id for vid in auto_mock.call_args.args[0]
    }
    assert candidate_ytids == {"AAA00000001"}
    emitted_titles = {v["title"] for v in emit_mock.call_args.args[1]}
    assert emitted_titles == {"Regular"}
    db.close()
