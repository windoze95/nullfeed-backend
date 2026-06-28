"""Tests for play-cache eviction (downloads-as-cache, #86).

Exercise the synchronous sweep directly (the way the Celery task does) against
an in-memory SQLite session plus real files under the temp media paths conftest
configures, mirroring tests/test_retention.py.
"""

import os
import uuid
from datetime import datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import Base
from app.models.user_queue import UserQueue
from app.models.user_video_ref import (
    REF_KIND_CACHE,
    REF_KIND_LIBRARY,
    UserVideoRef,
)
from app.models.video import Video
from app.services.cache_retention import enforce_cache_retention
from app.utils.time import utcnow_naive

CHANNEL_ID = "chan-1"


def _make_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _write(path: str, data: bytes = b"data") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _abs_media(rel_path: str) -> str:
    return os.path.join(settings.media_path, rel_path)


def _seed_downloaded_video(db) -> Video:
    vid_id = str(uuid.uuid4())
    yt_id = f"yt{uuid.uuid4().hex[:9]}"
    rel_path = f"{CHANNEL_ID}/{vid_id}.mp4"
    _write(_abs_media(rel_path))
    _write(os.path.join(settings.thumbnails_path, f"{yt_id}.jpg"), b"thumb")
    video = Video(
        id=vid_id,
        youtube_video_id=yt_id,
        channel_id=CHANNEL_ID,
        title="V",
        status="COMPLETE",
        file_path=rel_path,
        file_size_bytes=4,
    )
    db.add(video)
    db.commit()
    return video


def _seed_ref(
    db, user_id: str, video_id: str, kind: str, last_watched_at: datetime | None = None
) -> None:
    db.add(
        UserVideoRef(
            user_id=user_id,
            video_id=video_id,
            kind=kind,
            last_watched_at=last_watched_at,
        )
    )
    db.commit()


def _ref(db, user_id: str, video_id: str) -> UserVideoRef:
    return db.execute(
        select(UserVideoRef).where(
            UserVideoRef.user_id == user_id,
            UserVideoRef.video_id == video_id,
        )
    ).scalar_one()


def test_keeps_newest_cache_and_reclaims_the_rest(monkeypatch):
    monkeypatch.setattr(settings, "cache_retention_count", 1)
    db = _make_db()
    base = utcnow_naive()
    v_old = _seed_downloaded_video(db)
    v_new = _seed_downloaded_video(db)
    _seed_ref(db, "u1", v_old.id, REF_KIND_CACHE, base - timedelta(days=2))
    _seed_ref(db, "u1", v_new.id, REF_KIND_CACHE, base - timedelta(days=1))

    old_media = _abs_media(v_old.file_path)
    new_media = _abs_media(v_new.file_path)

    result = enforce_cache_retention(db)

    assert result == {"users": 1, "refs_removed": 1, "reclaimed": 1}
    # Least-recently-watched cache evicted; newest kept.
    assert _ref(db, "u1", v_old.id).removed_at is not None
    assert _ref(db, "u1", v_new.id).removed_at is None
    assert not os.path.exists(old_media)
    assert os.path.exists(new_media)
    db.refresh(v_old)
    assert v_old.status == "CATALOGED"
    db.close()


def test_library_refs_are_never_evicted(monkeypatch):
    monkeypatch.setattr(settings, "cache_retention_count", 0)  # drop all cache
    db = _make_db()
    v_lib = _seed_downloaded_video(db)
    v_cache = _seed_downloaded_video(db)
    _seed_ref(db, "u1", v_lib.id, REF_KIND_LIBRARY)
    _seed_ref(db, "u1", v_cache.id, REF_KIND_CACHE)

    result = enforce_cache_retention(db)

    assert result["refs_removed"] == 1
    assert _ref(db, "u1", v_lib.id).removed_at is None  # library untouched
    assert _ref(db, "u1", v_cache.id).removed_at is not None  # cache evicted
    assert os.path.exists(_abs_media(v_lib.file_path))
    db.close()


def test_queued_cache_is_pinned(monkeypatch):
    monkeypatch.setattr(settings, "cache_retention_count", 0)  # would drop all
    db = _make_db()
    v_queued = _seed_downloaded_video(db)
    v_other = _seed_downloaded_video(db)
    _seed_ref(db, "u1", v_queued.id, REF_KIND_CACHE)
    _seed_ref(db, "u1", v_other.id, REF_KIND_CACHE)
    db.add(UserQueue(user_id="u1", video_id=v_queued.id))
    db.commit()

    enforce_cache_retention(db)

    # "Want to watch later" pins the queued cache video; the other is evicted.
    assert _ref(db, "u1", v_queued.id).removed_at is None
    assert _ref(db, "u1", v_other.id).removed_at is not None
    assert os.path.exists(_abs_media(v_queued.file_path))
    db.close()


def test_negative_count_disables_eviction(monkeypatch):
    monkeypatch.setattr(settings, "cache_retention_count", -1)
    db = _make_db()
    v = _seed_downloaded_video(db)
    _seed_ref(db, "u1", v.id, REF_KIND_CACHE)

    result = enforce_cache_retention(db)

    assert result["refs_removed"] == 0
    assert _ref(db, "u1", v.id).removed_at is None
    db.close()


def test_shared_cache_not_reclaimed_while_another_ref_active(monkeypatch):
    monkeypatch.setattr(settings, "cache_retention_count", 0)
    db = _make_db()
    v = _seed_downloaded_video(db)
    # u1 holds it as cache (evicted); u2 holds it in their library (keeps file).
    _seed_ref(db, "u1", v.id, REF_KIND_CACHE)
    _seed_ref(db, "u2", v.id, REF_KIND_LIBRARY)

    result = enforce_cache_retention(db)

    assert result["refs_removed"] == 1
    assert result["reclaimed"] == 0
    assert _ref(db, "u1", v.id).removed_at is not None
    assert _ref(db, "u2", v.id).removed_at is None
    assert os.path.exists(_abs_media(v.file_path))  # shared file survives
    db.close()
