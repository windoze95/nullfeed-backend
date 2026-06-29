"""Tests for retention-policy enforcement (issue #12).

These exercise the synchronous sweep directly (the way the Celery task does),
against an in-memory SQLite session plus real files under the temp media paths
that conftest configures, so the full path is covered: soft-remove the refs a
policy drops, then reclaim via the shared orphan cleanup.
"""

import os
import uuid
from datetime import datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import Base
from app.models.subscription import UserSubscription
from app.models.user_video_ref import REF_KIND_CACHE, UserVideoRef
from app.models.video import Video
from app.services.retention import KEEP_ALL, KEEP_LAST_N, enforce_retention
from app.utils.time import utcnow_naive

CHANNEL_ID = "chan-1"


def _make_db():
    # In-memory SQLite without FK pragmas, so refs/subscriptions need no backing
    # user/channel rows (mirrors tests/test_session_reaper.py).
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _write(path: str, data: bytes = b"data") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _abs_media(rel_path: str) -> str:
    return os.path.join(settings.media_path, rel_path)


def _abs_thumb(youtube_video_id: str) -> str:
    return os.path.join(settings.thumbnails_path, f"{youtube_video_id}.jpg")


def _seed_downloaded_video(db, uploaded_at: datetime, channel_id: str = CHANNEL_ID):
    """Create a COMPLETE video with a media file and thumbnail on disk."""
    vid_id = str(uuid.uuid4())
    yt_id = f"yt{uuid.uuid4().hex[:9]}"
    rel_path = f"{channel_id}/{vid_id}.mp4"
    _write(_abs_media(rel_path))
    _write(_abs_thumb(yt_id), b"thumb")
    video = Video(
        id=vid_id,
        youtube_video_id=yt_id,
        channel_id=channel_id,
        title="V",
        status="COMPLETE",
        file_path=rel_path,
        file_size_bytes=4,
        uploaded_at=uploaded_at,
    )
    db.add(video)
    db.commit()
    return video


def _seed_cataloged_video(db, channel_id: str = CHANNEL_ID):
    """Create a cataloged (never-downloaded) video — no file on disk."""
    video = Video(
        id=str(uuid.uuid4()),
        youtube_video_id=f"yt{uuid.uuid4().hex[:9]}",
        channel_id=channel_id,
        title="V",
        status="CATALOGED",
    )
    db.add(video)
    db.commit()
    return video


def _seed_ref(db, user_id: str, video_id: str) -> None:
    # Followed-channel episodes are cached (CACHE refs); per-subscription
    # retention bounds those.
    db.add(UserVideoRef(user_id=user_id, video_id=video_id, kind=REF_KIND_CACHE))
    db.commit()


def _seed_sub(
    db, user_id: str, policy: str, count: int | None, channel_id: str = CHANNEL_ID
) -> None:
    db.add(
        UserSubscription(
            user_id=user_id,
            channel_id=channel_id,
            retention_policy=policy,
            retention_count=count,
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


def test_keep_last_n_keeps_newest_and_reclaims_the_rest():
    db = _make_db()
    base = utcnow_naive()
    v_old = _seed_downloaded_video(db, base - timedelta(days=3))
    v_mid = _seed_downloaded_video(db, base - timedelta(days=2))
    v_new = _seed_downloaded_video(db, base - timedelta(days=1))
    for v in (v_old, v_mid, v_new):
        _seed_ref(db, "u1", v.id)
    _seed_sub(db, "u1", KEEP_LAST_N, 2)

    # Capture on-disk paths before the sweep nulls file_path on reclaimed rows.
    old_media = _abs_media(v_old.file_path)
    old_thumb = _abs_thumb(v_old.youtube_video_id)
    mid_media = _abs_media(v_mid.file_path)
    new_media = _abs_media(v_new.file_path)

    result = enforce_retention(db)

    assert result == {"subscriptions": 1, "refs_removed": 1, "reclaimed": 1}

    # Oldest dropped: ref soft-removed, file + thumb gone, row reset.
    assert _ref(db, "u1", v_old.id).removed_at is not None
    assert not os.path.exists(old_media)
    assert not os.path.exists(old_thumb)
    db.refresh(v_old)
    assert v_old.status == "CATALOGED"
    assert v_old.file_path is None
    assert v_old.file_size_bytes is None

    # The newest two are untouched.
    assert _ref(db, "u1", v_mid.id).removed_at is None
    assert _ref(db, "u1", v_new.id).removed_at is None
    assert os.path.exists(mid_media)
    assert os.path.exists(new_media)
    db.refresh(v_mid)
    db.refresh(v_new)
    assert v_mid.status == "COMPLETE"
    assert v_new.status == "COMPLETE"
    db.close()


def test_keep_all_is_a_noop():
    db = _make_db()
    base = utcnow_naive()
    videos = [_seed_downloaded_video(db, base - timedelta(days=i)) for i in range(3)]
    for v in videos:
        _seed_ref(db, "u1", v.id)
    _seed_sub(db, "u1", KEEP_ALL, None)

    media_paths = [_abs_media(v.file_path) for v in videos]
    result = enforce_retention(db)

    # KEEP_ALL subscriptions are filtered out entirely; nothing happens.
    assert result == {"subscriptions": 0, "refs_removed": 0, "reclaimed": 0}
    for v, path in zip(videos, media_paths):
        assert _ref(db, "u1", v.id).removed_at is None
        assert os.path.exists(path)
        db.refresh(v)
        assert v.status == "COMPLETE"
    db.close()


def test_shared_download_is_not_reclaimed_while_another_ref_is_active():
    db = _make_db()
    base = utcnow_naive()
    v_old = _seed_downloaded_video(db, base - timedelta(days=2))
    v_new = _seed_downloaded_video(db, base - timedelta(days=1))
    # u1 keeps only the newest (KEEP_LAST_N=1), so v_old drops for u1; u2 still
    # wants v_old and holds an active ref.
    _seed_ref(db, "u1", v_old.id)
    _seed_ref(db, "u1", v_new.id)
    _seed_ref(db, "u2", v_old.id)
    _seed_sub(db, "u1", KEEP_LAST_N, 1)

    old_media = _abs_media(v_old.file_path)
    result = enforce_retention(db)

    assert result == {"subscriptions": 1, "refs_removed": 1, "reclaimed": 0}
    # u1's ref dropped, u2's ref intact, and the shared file survives.
    assert _ref(db, "u1", v_old.id).removed_at is not None
    assert _ref(db, "u2", v_old.id).removed_at is None
    assert os.path.exists(old_media)
    db.refresh(v_old)
    assert v_old.status == "COMPLETE"
    assert v_old.file_path is not None
    db.close()


def test_only_downloaded_videos_count_toward_retention():
    db = _make_db()
    base = utcnow_naive()
    v_dl_old = _seed_downloaded_video(db, base - timedelta(days=3))
    v_dl_new = _seed_downloaded_video(db, base - timedelta(days=1))
    v_cat = _seed_cataloged_video(db)  # never downloaded — no file
    for v in (v_dl_old, v_dl_new, v_cat):
        _seed_ref(db, "u1", v.id)
    _seed_sub(db, "u1", KEEP_LAST_N, 1)

    result = enforce_retention(db)

    # Only the older downloaded video counts against N=1; the cataloged ref is
    # left completely alone (we never touch videos the user hasn't downloaded).
    assert result == {"subscriptions": 1, "refs_removed": 1, "reclaimed": 1}
    assert _ref(db, "u1", v_dl_old.id).removed_at is not None
    assert _ref(db, "u1", v_dl_new.id).removed_at is None
    assert _ref(db, "u1", v_cat.id).removed_at is None
    db.refresh(v_cat)
    assert v_cat.status == "CATALOGED"
    db.close()


def test_keep_last_n_without_a_count_is_a_noop():
    db = _make_db()
    base = utcnow_naive()
    v1 = _seed_downloaded_video(db, base - timedelta(days=2))
    v2 = _seed_downloaded_video(db, base - timedelta(days=1))
    _seed_ref(db, "u1", v1.id)
    _seed_ref(db, "u1", v2.id)
    _seed_sub(db, "u1", KEEP_LAST_N, None)  # misconfigured: no count

    result = enforce_retention(db)

    # A policy with no positive count can't be enforced — treat as no-op rather
    # than deleting everything.
    assert result == {"subscriptions": 1, "refs_removed": 0, "reclaimed": 0}
    assert _ref(db, "u1", v1.id).removed_at is None
    assert _ref(db, "u1", v2.id).removed_at is None
    assert os.path.exists(_abs_media(v1.file_path))
    assert os.path.exists(_abs_media(v2.file_path))
    db.close()
