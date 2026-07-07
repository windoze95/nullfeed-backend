"""Tests for the parallel per-channel poll fan-out.

The periodic beat no longer polls every due channel sequentially on one
session; it enumerates the due channel ids (cheap, indexed) and dispatches a
Celery GROUP of per-channel ``poll_channel_task`` jobs, each on its OWN DB
session. These tests drive that fan-out in eager mode (``task_always_eager``,
so the group executes inline) over a shared in-memory SQLite, and cover:

* the beat dispatches exactly the due channels (and all of them when forced);
* one channel's failure is isolated — a healthy channel still polls and
  reschedules while the broken one backs off instead of hot-looping;
* the batched ref-ensure issues a bounded number of queries.
"""

import uuid

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.channel import Channel
from app.models.subscription import UserSubscription
from app.models.user_video_ref import UserVideoRef
from app.models.video import Video
from app.services import channel_poller
from app.utils.time import utcnow_naive

_UC_ID = "UCabc1230000000000000000"


@pytest.fixture
def eager_celery(monkeypatch):
    """Run tasks (and the dispatched group) inline instead of via the broker."""
    from app.tasks.celery_app import celery_app

    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(celery_app.conf, "task_eager_propagates", True)
    return celery_app


@pytest.fixture
def task_db(monkeypatch):
    """A shared in-memory DB wired into the Celery tasks' ``_get_sync_db``.

    StaticPool keeps a single connection so every session the eager tasks open
    sees the same data. No SQLite pragmas are registered (foreign keys stay
    off), matching the other poller unit tests, so subscriptions and refs can be
    seeded without real user rows.
    """
    import app.tasks.download_tasks as dt

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine)
    monkeypatch.setattr(dt, "_get_sync_db", lambda: session_local())
    # Never touch the real download path during eager fan-out.
    from unittest.mock import MagicMock

    monkeypatch.setattr(dt.download_video_task, "delay", MagicMock())
    return session_local


def _cadence(monkeypatch, *, floor=15, cap=240, factor=2.0):
    from app.config import settings

    monkeypatch.setattr(settings, "poll_interval_floor_minutes", floor)
    monkeypatch.setattr(settings, "poll_interval_cap_minutes", cap)
    monkeypatch.setattr(settings, "poll_interval_backoff_factor", factor)


def _seed_channel(session_local, *, next_poll_at, **overrides) -> Channel:
    db = session_local()
    suffix = uuid.uuid4().hex[:8]
    channel = Channel(
        id=str(uuid.uuid4()),
        youtube_channel_id=f"{_UC_ID}{suffix}",
        name="Test",
        slug=f"test-{suffix}",
        next_poll_at=next_poll_at,
        poll_interval_minutes=overrides.pop("poll_interval_minutes", 30),
        last_checked_at=overrides.pop("last_checked_at", None),
    )
    for key, value in overrides.items():
        setattr(channel, key, value)
    db.add(channel)
    db.add(UserSubscription(user_id="u1", channel_id=channel.id))
    db.commit()
    db.refresh(channel)
    db.close()
    return channel


# --- fan-out enumeration ----------------------------------------------------


def test_beat_dispatches_one_job_per_due_channel(eager_celery, task_db, monkeypatch):
    """due_only fans out a job for each due channel and skips the rest."""
    import app.tasks.download_tasks as dt

    due_a = _seed_channel(task_db, next_poll_at=_past())
    due_b = _seed_channel(task_db, next_poll_at=_past())
    not_due = _seed_channel(task_db, next_poll_at=_future())

    polled: list[str] = []

    def record(channel_id, _db):
        polled.append(channel_id)
        return {"cataloged_ids": [], "auto_download_ids": []}

    monkeypatch.setattr(dt, "poll_single_channel", record)

    result = dt.poll_all_channels_task.apply_async(kwargs={"due_only": True}).get()

    assert result == {"status": "ok", "dispatched": 2}
    assert set(polled) == {due_a.id, due_b.id}
    assert not_due.id not in polled


def test_force_poll_dispatches_every_channel(eager_celery, task_db, monkeypatch):
    """due_only=False (pull-to-refresh poll-all) fans out for every channel."""
    import app.tasks.download_tasks as dt

    due = _seed_channel(task_db, next_poll_at=_past())
    not_due = _seed_channel(task_db, next_poll_at=_future())

    polled: list[str] = []
    monkeypatch.setattr(
        dt,
        "poll_single_channel",
        lambda cid, _db: (
            polled.append(cid) or {"cataloged_ids": [], "auto_download_ids": []}
        ),
    )

    result = dt.poll_all_channels_task.apply_async(kwargs={"due_only": False}).get()

    assert result == {"status": "ok", "dispatched": 2}
    assert set(polled) == {due.id, not_due.id}


def test_beat_with_no_due_channels_dispatches_nothing(eager_celery, task_db):
    import app.tasks.download_tasks as dt

    _seed_channel(task_db, next_poll_at=_future())

    result = dt.poll_all_channels_task.apply_async(kwargs={"due_only": True}).get()

    assert result == {"status": "ok", "dispatched": 0}


# --- isolation: one stuck channel must not block/abort the others -----------


def test_failing_channel_job_does_not_abort_others(eager_celery, task_db, monkeypatch):
    """A channel whose poll raises is isolated: the healthy channel still polls
    and reschedules toward the floor, while the broken one backs off toward the
    cap instead of hot-looping the beat."""
    _cadence(monkeypatch, floor=15, cap=240, factor=2.0)
    import app.tasks.download_tasks as dt

    good = _seed_channel(
        task_db,
        next_poll_at=_past(),
        poll_interval_minutes=30,
        last_checked_at=utcnow_naive(),
    )
    bad = _seed_channel(
        task_db,
        next_poll_at=_past(),
        poll_interval_minutes=30,
        last_checked_at=utcnow_naive(),
    )

    good_yt = good.youtube_channel_id
    new_vid = "GOODNEW0001"

    def fake_rss(channel_id, etag=None, last_modified=None):
        if channel_id == good_yt:
            return {
                "status": "ok",
                "entries": [{"youtube_video_id": new_vid}],
                "etag": None,
                "last_modified": None,
            }
        raise RuntimeError("yt-dlp/RSS hung for this channel")

    monkeypatch.setattr(channel_poller, "fetch_channel_rss", fake_rss)
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
    from unittest.mock import MagicMock

    monkeypatch.setattr(channel_poller, "publish_new_episode", MagicMock())

    before = utcnow_naive()
    dt.poll_all_channels_task.apply_async(kwargs={"due_only": True}).get()

    db = task_db()
    try:
        # The healthy channel polled despite the other failing: its new video is
        # cataloged and its cadence shortened toward the floor.
        good_after = db.get(Channel, good.id)
        good_videos = (
            db.execute(select(Video).where(Video.channel_id == good.id)).scalars().all()
        )
        assert [v.youtube_video_id for v in good_videos] == [new_vid]
        assert good_after.poll_interval_minutes == 15  # 30 / 2, found_new
        assert good_after.next_poll_at > before

        # The broken channel cataloged nothing and was backed off (interval
        # widened, next poll pushed into the future) rather than left due.
        bad_after = db.get(Channel, bad.id)
        bad_videos = (
            db.execute(select(Video).where(Video.channel_id == bad.id)).scalars().all()
        )
        assert bad_videos == []
        assert bad_after.poll_interval_minutes == 60  # 30 * 2, backed off
        assert bad_after.next_poll_at > before
    finally:
        db.close()

    # The healthy channel's new upload was enqueued for auto-download exactly
    # once; the failed channel enqueued nothing.
    assert dt.download_video_task.delay.call_count == 1
    (enqueued_id,) = dt.download_video_task.delay.call_args.args
    assert enqueued_id == good_videos[0].id


def test_per_channel_job_reschedules_its_own_channel(
    eager_celery, task_db, monkeypatch
):
    """A single due channel polled via the fan-out reschedules itself (304 ->
    empty poll -> cadence widens, last_checked stamped, next poll in future)."""
    _cadence(monkeypatch, floor=15, cap=240, factor=2.0)
    import app.tasks.download_tasks as dt

    ch = _seed_channel(
        task_db,
        next_poll_at=_past(),
        poll_interval_minutes=60,
        last_checked_at=utcnow_naive(),
    )
    monkeypatch.setattr(
        channel_poller, "fetch_channel_rss", lambda *a, **k: {"status": "not_modified"}
    )

    before = utcnow_naive()
    result = dt.poll_all_channels_task.apply_async(kwargs={"due_only": True}).get()
    assert result == {"status": "ok", "dispatched": 1}

    db = task_db()
    try:
        after = db.get(Channel, ch.id)
        assert after.poll_interval_minutes == 120  # 60 * 2, empty poll
        assert after.next_poll_at > before
        assert after.last_checked_at is not None
    finally:
        db.close()


# --- batched ref-ensure: bounded query count --------------------------------


@pytest.mark.parametrize("n_subs,n_videos", [(1, 1), (3, 5), (8, 8)])
def test_ensure_user_refs_bulk_is_bounded(n_subs, n_videos):
    """The batched ref-ensure issues a constant 2 SELECTs regardless of how many
    subscribers or videos it covers (vs the old per-video, per-subscriber N+1)."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    selects: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    db = sessionmaker(bind=engine)()
    channel_id = str(uuid.uuid4())
    db.add(
        Channel(
            id=channel_id,
            youtube_channel_id=_UC_ID,
            name="Test",
            slug="test",
        )
    )
    for i in range(n_subs):
        db.add(UserSubscription(user_id=f"u{i}", channel_id=channel_id))
    video_ids = []
    for i in range(n_videos):
        vid_id = str(uuid.uuid4())
        db.add(
            Video(
                id=vid_id,
                youtube_video_id=f"vid{i:08d}",
                channel_id=channel_id,
                title=f"v{i}",
                status="CATALOGED",
            )
        )
        video_ids.append(vid_id)
    db.commit()

    # Ids captured as plain strings above, so passing them triggers no
    # post-commit attribute reload that would skew the SELECT count.
    selects.clear()
    channel_poller._ensure_user_refs_bulk(video_ids, channel_id, db)
    db.flush()

    # One SELECT for subscriber ids + one for existing (subscriber x video)
    # pairs. Bounded — independent of n_subs and n_videos.
    assert len(selects) == 2

    refs = db.execute(select(UserVideoRef)).scalars().all()
    assert len(refs) == n_subs * n_videos
    db.close()


# --- helpers ----------------------------------------------------------------


def _past():
    from datetime import timedelta

    return utcnow_naive() - timedelta(minutes=1)


def _future():
    from datetime import timedelta

    return utcnow_naive() + timedelta(hours=2)
