"""Unplayable-reason detection: classify why YouTube refuses a video (age gate,
members-only, premium, private, geo block, removed, DRM, unaired premiere),
persist it on the row, surface it via the API, and clear it on any success."""

import json
import subprocess
import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.api.videos as videos_api
from app.database import async_session_factory
from app.models import Base
from app.models.channel import Channel
from app.models.video import Video
from app.services import channel_poller, download_manager
from app.services.download_manager import (
    download_video,
    fetch_channel_videos,
    fetch_videos_metadata,
)
from app.services.instant_stream import InstantStreamError
from app.utils.unplayable import (
    AGE_RESTRICTED,
    MEMBERS_ONLY,
    PREMIUM,
    PRIVATE,
    REMOVED,
    UPCOMING,
    SOFT_REASONS,
    UnplayableVideoError,
    classify_availability,
    classify_extraction_error,
    classify_live_status,
    extract_error_text,
)
from tests.helpers import fake_completed_process, seed_channel, seed_video

# --- classifier ------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # Real yt-dlp / YouTube playability strings, one per reason.
        (
            "ERROR: [youtube] abc: Sign in to confirm your age. "
            "This video may be inappropriate for some users.",
            "age_restricted",
        ),
        (
            "ERROR: [youtube] abc: Join this channel to get access to "
            "members-only content like this video, and other exclusive perks.",
            "members_only",
        ),
        ("ERROR: [youtube] abc: This video requires payment to watch.", "premium"),
        (
            "ERROR: [youtube] abc: This video is available to Music Premium "
            "members only",
            "premium",
        ),
        (
            "ERROR: [youtube] abc: Private video. Sign in if you've been "
            "granted access to this video",
            "private",
        ),
        (
            "ERROR: [youtube] abc: The uploader has not made this video "
            "available in your country",
            "geo_blocked",
        ),
        ("ERROR: [youtube] abc: This video is DRM protected", "drm"),
        ("ERROR: [youtube] abc: Premieres in 3 hours", "upcoming"),
        ("ERROR: [youtube] abc: This live event will begin in 2 hours", "upcoming"),
        (
            "ERROR: [youtube] abc: Video unavailable. This video has been "
            "removed by the uploader",
            "removed",
        ),
        (
            "ERROR: [youtube] abc: This video is no longer available because "
            "the YouTube account associated with this video has been terminated.",
            "removed",
        ),
        ("ERROR: [youtube] abc: Video unavailable", "unavailable"),
    ],
)
def test_classify_recognizes_permanent_reasons(message, expected):
    assert classify_extraction_error(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        # Session/infrastructure trouble must never label the video.
        "ERROR: [youtube] abc: Sign in to confirm you're not a bot.",
        "ERROR: [youtube] abc: This content isn't available, try again later.",
        "ERROR: [youtube] abc: YouTube is requiring a captcha challenge",
        "ERROR: unable to download webpage: HTTP Error 500",
        "ERROR: [youtube] abc: Requested format is not available",
        "some random failure",
        "",
        None,
    ],
)
def test_classify_ignores_transient_and_unknown(message):
    assert classify_extraction_error(message) is None


def test_classify_availability_maps_gated_values():
    assert classify_availability("subscriber_only") == MEMBERS_ONLY
    assert classify_availability("premium_only") == PREMIUM
    assert classify_availability("private") == PRIVATE
    # needs_auth means extraction *succeeded* while age-gated -> playable here.
    assert classify_availability("needs_auth") is None
    assert classify_availability("public") is None
    assert classify_availability(None) is None


def test_classify_live_status_only_flags_upcoming():
    assert classify_live_status("is_upcoming") == UPCOMING
    assert classify_live_status("is_live") is None
    assert classify_live_status(None) is None


def test_extract_error_text_prefers_error_line_over_trailing_noise():
    blob = (
        "[download] 12.3%\n"
        "ERROR: [youtube] abc: Join this channel to get access to members-only "
        "content\n"
        "[aria2c] shutting down\n"
    )
    assert extract_error_text(blob).startswith("ERROR: [youtube] abc: Join")
    assert extract_error_text("") == ""


# --- poll-time labeling ------------------------------------------------------


def test_fetch_channel_videos_labels_flat_entries(monkeypatch):
    entries = [
        {"id": "AAA00000001", "title": "Public", "duration": 10},
        {
            "id": "BBB00000002",
            "title": "Members",
            "duration": 20,
            "availability": "subscriber_only",
        },
        {
            "id": "CCC00000003",
            "title": "Premiere",
            "duration": 0,
            "live_status": "is_upcoming",
        },
    ]
    stdout = "\n".join(json.dumps(e) for e in entries)
    monkeypatch.setattr(
        download_manager.subprocess,
        "run",
        lambda *a, **k: fake_completed_process(stdout),
    )

    videos = fetch_channel_videos("UCabc")["videos"]

    assert [v["unplayable_reason"] for v in videos] == [None, MEMBERS_ONLY, UPCOMING]


def test_fetch_videos_metadata_stubs_unplayable_ids(monkeypatch):
    ok_entry = {"id": "AAA00000001", "title": "Public", "duration": 10}
    stderr = (
        "ERROR: [youtube] BBB00000002: Join this channel to get access to "
        "members-only content like this video, and other exclusive perks.\n"
        "ERROR: [youtube] CCC00000003: Sign in to confirm you're not a bot.\n"
    )
    monkeypatch.setattr(
        download_manager.subprocess,
        "run",
        lambda *a, **k: fake_completed_process(
            json.dumps(ok_entry), returncode=1, stderr=stderr
        ),
    )

    result = fetch_videos_metadata(
        ["AAA00000001", "BBB00000002", "CCC00000003"],
        titles={"BBB00000002": "Members Episode"},
    )

    # Order preserved; the members-only id is a labeled stub with the RSS
    # title; the bot-check (transient) id is omitted as before.
    assert [v["youtube_video_id"] for v in result] == ["AAA00000001", "BBB00000002"]
    assert result[0]["unplayable_reason"] is None
    assert result[1]["unplayable_reason"] == MEMBERS_ONLY
    assert result[1]["title"] == "Members Episode"


def test_fetch_videos_metadata_stub_title_falls_back_to_id(monkeypatch):
    stderr = "ERROR: [youtube] BBB00000002: This video requires payment to watch.\n"
    monkeypatch.setattr(
        download_manager.subprocess,
        "run",
        lambda *a, **k: fake_completed_process("", returncode=1, stderr=stderr),
    )

    result = fetch_videos_metadata(["BBB00000002"])

    assert len(result) == 1
    assert result[0]["title"] == "BBB00000002"
    assert result[0]["unplayable_reason"] == PREMIUM


# --- cataloging --------------------------------------------------------------


def _mem_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_catalog_videos_labels_and_gates_hard_blocked(monkeypatch):
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
        {"youtube_video_id": "AAA00000001", "title": "Public"},
        {
            "youtube_video_id": "BBB00000002",
            "title": "Members",
            "unplayable_reason": MEMBERS_ONLY,
        },
        {
            "youtube_video_id": "CCC00000003",
            "title": "Age gated",
            "unplayable_reason": AGE_RESTRICTED,
        },
    ]
    result = channel_poller._catalog_videos(
        channel, yt_videos, db, had_initial_poll=True, update_schedule=False
    )

    # All three are cataloged (visibility is the point)…
    assert len(result["cataloged_ids"]) == 3
    reasons = dict(
        db.execute(select(Video.youtube_video_id, Video.unplayable_reason)).all()
    )
    assert reasons == {
        "AAA00000001": None,
        "BBB00000002": MEMBERS_ONLY,
        "CCC00000003": AGE_RESTRICTED,
    }

    # …but the hard-blocked one is excluded from auto-download candidates and
    # from new-episode events; the soft (age-gated) one stays in both.
    candidate_ytids = {
        db.get(Video, vid).youtube_video_id for vid in auto_mock.call_args.args[0]
    }
    assert candidate_ytids == {"AAA00000001", "CCC00000003"}
    emitted_titles = {v["title"] for v in emit_mock.call_args.args[1]}
    assert emitted_titles == {"Public", "Age gated"}
    db.close()


# --- download task ------------------------------------------------------------


@pytest.fixture
def eager_celery(monkeypatch):
    from app.tasks.celery_app import celery_app

    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(celery_app.conf, "task_eager_propagates", True)
    return celery_app


def test_download_video_raises_unplayable_on_members_only(monkeypatch, tmp_path):
    monkeypatch.setattr(download_manager.settings, "media_path", str(tmp_path))
    monkeypatch.setattr(download_manager, "WATCHDOG_POLL_INTERVAL_SECONDS", 0.05)
    real_popen = subprocess.Popen
    script = (
        "echo 'ERROR: [youtube] vid123: Join this channel to get access to "
        "members-only content like this video, and other exclusive perks.'; "
        "exit 1"
    )
    monkeypatch.setattr(
        download_manager.subprocess,
        "Popen",
        lambda cmd, **kwargs: real_popen(["sh", "-c", script], **kwargs),
    )

    with pytest.raises(UnplayableVideoError) as excinfo:
        download_video("vid123", "test-slug")

    assert excinfo.value.reason == MEMBERS_ONLY


@pytest.mark.asyncio
async def test_download_task_records_reason_without_retry(
    client, make_user, monkeypatch, eager_celery
):
    await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="PENDING")
    vid = video.id

    import app.tasks.download_tasks as dt

    attempts = []

    def refused(**kwargs):
        attempts.append(1)
        raise UnplayableVideoError(MEMBERS_ONLY, "yt-dlp failed: members only")

    monkeypatch.setattr(dt, "download_video", refused)

    result = dt.download_video_task.delay(vid).get()

    assert result == {"status": "unplayable", "reason": MEMBERS_ONLY, "video_id": vid}
    assert attempts == [1]  # outside autoretry_for -> exactly one attempt
    async with async_session_factory() as db:
        row = (await db.execute(select(Video).where(Video.id == vid))).scalars().one()
    assert row.status == "FAILED"
    assert row.unplayable_reason == MEMBERS_ONLY


@pytest.mark.asyncio
async def test_download_task_success_clears_stale_reason(
    client, make_user, monkeypatch, eager_celery, tmp_path
):
    await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(
            db, channel, status="PENDING", unplayable_reason=AGE_RESTRICTED
        )
    vid = video.id

    import app.tasks.download_tasks as dt

    monkeypatch.setattr(dt.settings, "media_path", str(tmp_path))
    monkeypatch.setattr(
        dt,
        "download_video",
        lambda **kwargs: {
            "file_path": "slug/vid.mp4",
            "file_size_bytes": 123,
            "title": "Now playable",
            "duration_seconds": 60,
            "uploaded_at": None,
            "metadata_json": None,
        },
    )

    result = dt.download_video_task.delay(vid).get()

    assert result["status"] == "complete"
    async with async_session_factory() as db:
        row = (await db.execute(select(Video).where(Video.id == vid))).scalars().one()
    assert row.status == "COMPLETE"
    assert row.unplayable_reason is None


@pytest.mark.asyncio
async def test_preview_task_records_reason_and_resets_status(
    client, make_user, monkeypatch, eager_celery
):
    user, _ = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="CATALOGED")
    vid = video.id

    import app.tasks.download_tasks as dt

    def refused(**kwargs):
        raise UnplayableVideoError(
            MEMBERS_ONLY, "Preview download failed for x: members only"
        )

    monkeypatch.setattr(dt, "download_preview", refused)

    result = dt.download_preview_task.delay(vid, user["id"]).get()

    assert result == {"status": "unplayable", "reason": MEMBERS_ONLY, "video_id": vid}
    async with async_session_factory() as db:
        row = (await db.execute(select(Video).where(Video.id == vid))).scalars().one()
    assert row.preview_status is None
    assert row.unplayable_reason == MEMBERS_ONLY


# --- prewarm gating -----------------------------------------------------------


@pytest.mark.asyncio
async def test_prewarm_skips_hard_unplayable_allows_soft(
    client, make_user, monkeypatch
):
    _, headers = await make_user()
    delay_mock = MagicMock()
    monkeypatch.setattr("app.api.videos.download_preview_task.delay", delay_mock)
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        members = await seed_video(
            db, channel, status="CATALOGED", unplayable_reason=MEMBERS_ONLY
        )
        age_gated = await seed_video(
            db, channel, status="CATALOGED", unplayable_reason=AGE_RESTRICTED
        )
        plain = await seed_video(db, channel, status="CATALOGED")

    resp = await client.post(
        "/api/videos/prewarm",
        json={"video_ids": [members.id, age_gated.id, plain.id]},
        headers=headers,
    )

    assert resp.status_code == 200
    assert set(resp.json()["enqueued"]) == {age_gated.id, plain.id}
    assert AGE_RESTRICTED in SOFT_REASONS  # the invariant the filter relies on


# --- instant-stream endpoint ---------------------------------------------------


@pytest.mark.asyncio
async def test_instant_stream_persists_classified_reason(
    client, make_user, monkeypatch
):
    _, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="CATALOGED")

    def refused(vid: str) -> str:
        raise InstantStreamError("Resolve failed for x: members", reason=MEMBERS_ONLY)

    monkeypatch.setattr(videos_api, "resolve_progressive_url", refused)

    resp = await client.get(f"/api/videos/{video.id}/instant-stream", headers=headers)

    assert resp.status_code == 502
    assert MEMBERS_ONLY in resp.json()["detail"]
    async with async_session_factory() as db:
        reason = (
            await db.execute(
                select(Video.unplayable_reason).where(Video.id == video.id)
            )
        ).scalar_one()
    assert reason == MEMBERS_ONLY


@pytest.mark.asyncio
async def test_instant_stream_unclassified_failure_leaves_video_unlabeled(
    client, make_user, monkeypatch
):
    _, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="CATALOGED")

    def flaky(vid: str) -> str:
        raise InstantStreamError("Resolve timed out for x")

    monkeypatch.setattr(videos_api, "resolve_progressive_url", flaky)

    resp = await client.get(f"/api/videos/{video.id}/instant-stream", headers=headers)

    assert resp.status_code == 502
    async with async_session_factory() as db:
        reason = (
            await db.execute(
                select(Video.unplayable_reason).where(Video.id == video.id)
            )
        ).scalar_one()
    assert reason is None


@pytest.mark.asyncio
async def test_instant_stream_success_clears_stale_reason(
    client, make_user, monkeypatch
):
    _, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(
            db, channel, status="CATALOGED", unplayable_reason=UPCOMING
        )

    monkeypatch.setattr(
        videos_api, "resolve_progressive_url", lambda vid: "https://upstream.test/v"
    )

    async def fake_proxy(url, range_header):
        from fastapi.responses import Response

        return Response(content=b"ok", media_type="video/mp4")

    monkeypatch.setattr(videos_api, "stream_proxy", fake_proxy)

    resp = await client.get(f"/api/videos/{video.id}/instant-stream", headers=headers)

    assert resp.status_code == 200
    async with async_session_factory() as db:
        reason = (
            await db.execute(
                select(Video.unplayable_reason).where(Video.id == video.id)
            )
        ).scalar_one()
    assert reason is None


# --- API surface ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_video_detail_exposes_unplayable_reason(client, make_user):
    _, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(
            db, channel, status="FAILED", unplayable_reason=MEMBERS_ONLY
        )

    resp = await client.get(f"/api/videos/{video.id}", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["unplayable_reason"] == MEMBERS_ONLY


@pytest.mark.asyncio
async def test_channel_videos_expose_unplayable_reason(client, make_user):
    _, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        await seed_video(db, channel, unplayable_reason=REMOVED)

    resp = await client.get(f"/api/channels/{channel.id}/videos", headers=headers)

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items and items[0]["unplayable_reason"] == REMOVED
