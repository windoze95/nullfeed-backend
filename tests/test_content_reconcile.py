"""Content reconciliation — the back-catalog catch-up job: discover a channel's
Shorts/livestreams and classify content_type-NULL rows (from stored metadata, or
a batched extraction), bounded and self-limiting."""

import uuid
from unittest.mock import MagicMock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.models.channel import Channel
from app.models.subscription import UserSubscription
from app.models.video import Video
from app.services import channel_poller
from app.services.channel_poller import (
    channels_needing_reconcile,
    reconcile_channel_content,
)
from app.utils.content_type import MEMBERS_ONLY, SHORT


def _mem_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _channel(db, ytid="UCabc1230000000000000000"):
    ch = Channel(id=str(uuid.uuid4()), youtube_channel_id=ytid, name="T", slug=ytid)
    db.add(ch)
    db.commit()
    return ch


def _video(db, channel, *, content_type=None, metadata_json=None, ytid=None):
    video = Video(
        id=str(uuid.uuid4()),
        youtube_video_id=ytid or f"yt{uuid.uuid4().hex[:9]}",
        channel_id=channel.id,
        title="V",
        status="CATALOGED",
        content_type=content_type,
        metadata_json=metadata_json,
    )
    db.add(video)
    db.commit()
    return video


def test_channels_needing_reconcile_only_those_with_null_rows():
    db = _mem_session()
    done = _channel(db, "UCdone000000000000000000")
    todo = _channel(db, "UCtodo000000000000000000")
    for ch in (done, todo):
        db.add(UserSubscription(user_id="u1", channel_id=ch.id))
    db.commit()
    _video(db, done, content_type="regular")  # fully typed → drops out
    _video(db, todo, content_type=None)  # still needs classifying

    assert channels_needing_reconcile(db, limit=10) == [todo.id]
    db.close()


def test_reconcile_classifies_from_stored_metadata(monkeypatch):
    db = _mem_session()
    monkeypatch.setattr(channel_poller, "fetch_channel_tab", lambda *a, **k: [])
    ch = _channel(db)
    video = _video(
        db, ch, content_type=None, metadata_json={"availability": "subscriber_only"}
    )

    result = reconcile_channel_content(ch.id, db)

    db.refresh(video)
    assert video.content_type == MEMBERS_ONLY  # free, from stored metadata
    assert result["classified"] == 1
    db.close()


def test_reconcile_fetches_when_no_stored_metadata(monkeypatch):
    db = _mem_session()
    monkeypatch.setattr(channel_poller, "fetch_channel_tab", lambda *a, **k: [])
    monkeypatch.setattr(
        channel_poller,
        "fetch_videos_metadata",
        lambda ids: [{"youtube_video_id": "ABC00000001", "content_type": SHORT}],
    )
    ch = _channel(db)
    video = _video(db, ch, content_type=None, metadata_json=None, ytid="ABC00000001")

    reconcile_channel_content(ch.id, db)

    db.refresh(video)
    assert video.content_type == SHORT
    db.close()


def test_reconcile_transient_fetch_failure_leaves_null(monkeypatch):
    db = _mem_session()
    monkeypatch.setattr(channel_poller, "fetch_channel_tab", lambda *a, **k: [])
    # The id isn't returned (bot-check / network) → stays NULL, retried next pass.
    monkeypatch.setattr(channel_poller, "fetch_videos_metadata", lambda ids: [])
    ch = _channel(db)
    video = _video(db, ch, content_type=None, metadata_json=None)

    reconcile_channel_content(ch.id, db)

    db.refresh(video)
    assert video.content_type is None
    db.close()


def test_reconcile_discovers_shorts(monkeypatch):
    db = _mem_session()
    monkeypatch.setattr(channel_poller, "_emit_new_episode_events", MagicMock())
    monkeypatch.setattr(
        channel_poller, "_determine_auto_downloads", MagicMock(return_value=[])
    )
    monkeypatch.setattr(channel_poller, "fetch_videos_metadata", lambda ids: [])

    def fake_tab(cid, tab, default_type, *a, **k):
        if tab == "/shorts":
            return [
                {
                    "youtube_video_id": "SHORT000001",
                    "title": "Clip",
                    "duration_seconds": 30,
                    "upload_date": None,
                    "unplayable_reason": None,
                    "content_type": SHORT,
                }
            ]
        return []

    monkeypatch.setattr(channel_poller, "fetch_channel_tab", fake_tab)
    ch = _channel(db)

    result = reconcile_channel_content(ch.id, db)

    assert result["discovered"] == 1
    row = db.execute(
        select(Video).where(Video.youtube_video_id == "SHORT000001")
    ).scalar_one()
    assert row.content_type == SHORT
    db.close()
