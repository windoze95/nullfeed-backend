"""Tests for adaptive per-channel poll cadence.

Covers the reschedule algorithm (multiplicative backoff bounded by floor/cap),
due-only channel selection in poll_all_channels, the end-to-end cadence updates
from poll_single_channel, and the failed-poll backoff that keeps the frequent
beat from hot-looping a broken channel.
"""

import uuid
from datetime import timedelta
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.models.channel import Channel
from app.models.subscription import UserSubscription
from app.services import channel_poller
from app.utils.time import utcnow_naive

_UC_ID = "UCabc1230000000000000000"


def _cadence(monkeypatch, *, floor=15, cap=240, factor=2.0):
    """Pin the cadence settings so assertions are independent of the defaults."""
    from app.config import settings

    monkeypatch.setattr(settings, "poll_interval_floor_minutes", floor)
    monkeypatch.setattr(settings, "poll_interval_cap_minutes", cap)
    monkeypatch.setattr(settings, "poll_interval_backoff_factor", factor)


def _bare_channel(**overrides) -> Channel:
    """A Channel instance with no DB session (for pure reschedule tests)."""
    ch = Channel(id="c-1", youtube_channel_id="UCx", name="x", slug="x")
    for key, value in overrides.items():
        setattr(ch, key, value)
    return ch


def _mem_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _add_channel(
    db,
    *,
    next_poll_at,
    poll_interval_minutes=15,
    youtube_channel_id=_UC_ID,
    last_checked_at=None,
    subscriber="u1",
) -> Channel:
    suffix = uuid.uuid4().hex[:8]
    channel = Channel(
        id=str(uuid.uuid4()),
        youtube_channel_id=f"{youtube_channel_id}{suffix}",
        name="Test",
        slug=f"test-{suffix}",
        next_poll_at=next_poll_at,
        poll_interval_minutes=poll_interval_minutes,
        last_checked_at=last_checked_at,
    )
    db.add(channel)
    if subscriber:
        db.add(UserSubscription(user_id=subscriber, channel_id=channel.id))
    db.commit()
    return channel


# --- reschedule algorithm --------------------------------------------------


def test_reschedule_shortens_toward_floor_on_new_upload(monkeypatch):
    _cadence(monkeypatch)
    ch = _bare_channel(poll_interval_minutes=120)
    before = utcnow_naive()

    channel_poller._reschedule_channel(ch, found_new=True)

    assert ch.poll_interval_minutes == 60  # 120 / 2
    assert ch.next_poll_at >= before + timedelta(minutes=60) - timedelta(seconds=5)


def test_reschedule_lengthens_toward_cap_on_empty_poll(monkeypatch):
    _cadence(monkeypatch)
    ch = _bare_channel(poll_interval_minutes=60)
    before = utcnow_naive()

    channel_poller._reschedule_channel(ch, found_new=False)

    assert ch.poll_interval_minutes == 120  # 60 * 2
    assert ch.next_poll_at >= before + timedelta(minutes=120) - timedelta(seconds=5)


def test_reschedule_never_goes_below_floor(monkeypatch):
    _cadence(monkeypatch, floor=15, cap=240, factor=2.0)
    ch = _bare_channel(poll_interval_minutes=15)

    channel_poller._reschedule_channel(ch, found_new=True)

    assert ch.poll_interval_minutes == 15


def test_reschedule_never_exceeds_cap(monkeypatch):
    _cadence(monkeypatch, floor=15, cap=240, factor=2.0)
    ch = _bare_channel(poll_interval_minutes=240)

    channel_poller._reschedule_channel(ch, found_new=False)

    assert ch.poll_interval_minutes == 240


def test_reschedule_clamps_inverted_bounds(monkeypatch):
    # cap < floor is a misconfiguration; the floor must still win.
    _cadence(monkeypatch, floor=30, cap=10, factor=2.0)
    ch = _bare_channel(poll_interval_minutes=20)

    channel_poller._reschedule_channel(ch, found_new=False)

    assert ch.poll_interval_minutes == 30


# --- due-only selection in poll_all_channels -------------------------------


def test_due_only_polls_only_due_channels(monkeypatch):
    db = _mem_session()
    due = _add_channel(db, next_poll_at=utcnow_naive() - timedelta(minutes=1))
    _add_channel(db, next_poll_at=utcnow_naive() + timedelta(hours=2))

    polled: list[str] = []

    def fake_poll(channel_id, _db):
        polled.append(channel_id)
        return {"cataloged_ids": [], "auto_download_ids": []}

    monkeypatch.setattr(channel_poller, "poll_single_channel", fake_poll)

    channel_poller.poll_all_channels(db, due_only=True)

    assert polled == [due.id]
    db.close()


def test_force_poll_ignores_schedule_and_polls_all(monkeypatch):
    db = _mem_session()
    due = _add_channel(db, next_poll_at=utcnow_naive() - timedelta(minutes=1))
    not_due = _add_channel(db, next_poll_at=utcnow_naive() + timedelta(hours=2))

    polled: list[str] = []

    def fake_poll(channel_id, _db):
        polled.append(channel_id)
        return {"cataloged_ids": [], "auto_download_ids": []}

    monkeypatch.setattr(channel_poller, "poll_single_channel", fake_poll)

    channel_poller.poll_all_channels(db, due_only=False)

    assert set(polled) == {due.id, not_due.id}
    db.close()


def test_failed_poll_backs_off_instead_of_hot_looping(monkeypatch):
    _cadence(monkeypatch, floor=15, cap=240, factor=2.0)
    db = _mem_session()
    ch = _add_channel(
        db,
        next_poll_at=utcnow_naive() - timedelta(minutes=1),
        poll_interval_minutes=30,
    )

    def boom(channel_id, _db):
        raise RuntimeError("yt-dlp blew up")

    monkeypatch.setattr(channel_poller, "poll_single_channel", boom)
    before = utcnow_naive()

    channel_poller.poll_all_channels(db, due_only=True)

    db.refresh(ch)
    # Backed off like an empty poll (30 * 2) and pushed into the future so the
    # next beat won't immediately retry it.
    assert ch.poll_interval_minutes == 60
    assert ch.next_poll_at > before
    db.close()


# --- end-to-end cadence updates via poll_single_channel --------------------


def test_routine_304_poll_lengthens_cadence(monkeypatch):
    _cadence(monkeypatch, floor=15, cap=240, factor=2.0)
    db = _mem_session()
    ch = _add_channel(
        db,
        next_poll_at=utcnow_naive() - timedelta(minutes=1),
        poll_interval_minutes=60,
        last_checked_at=utcnow_naive(),
    )
    monkeypatch.setattr(
        channel_poller, "fetch_channel_rss", lambda *a, **k: {"status": "not_modified"}
    )
    before = utcnow_naive()

    channel_poller.poll_single_channel(ch.id, db)

    db.refresh(ch)
    assert ch.poll_interval_minutes == 120  # 60 * 2
    assert ch.next_poll_at >= before + timedelta(minutes=120) - timedelta(seconds=5)
    assert ch.last_checked_at is not None
    db.close()


def test_routine_new_upload_poll_shortens_cadence(monkeypatch):
    _cadence(monkeypatch, floor=15, cap=240, factor=2.0)
    db = _mem_session()
    ch = _add_channel(
        db,
        next_poll_at=utcnow_naive() - timedelta(minutes=1),
        poll_interval_minutes=120,
        last_checked_at=utcnow_naive(),
    )
    monkeypatch.setattr(
        channel_poller,
        "fetch_channel_rss",
        lambda *a, **k: {
            "status": "ok",
            "entries": [{"youtube_video_id": "NEW00000001"}],
            "etag": None,
            "last_modified": None,
        },
    )
    monkeypatch.setattr(
        channel_poller,
        "fetch_videos_metadata",
        lambda ids, titles=None: [
            {
                "youtube_video_id": vid,
                "title": vid,
                "duration_seconds": 0,
                "upload_date": None,
            }
            for vid in ids
        ],
    )
    monkeypatch.setattr(channel_poller, "publish_new_episode", MagicMock())

    result = channel_poller.poll_single_channel(ch.id, db)

    assert len(result["cataloged_ids"]) == 1
    db.refresh(ch)
    assert ch.poll_interval_minutes == 60  # 120 / 2
    db.close()
