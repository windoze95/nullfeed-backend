"""Tests for the YouTube WebSub (PubSubHubbub) subscriber.

Covered, all without network (httpx mocked) and with WebSub off by default:

* parsing: ``parse_push`` extracts video ids + channel id; ``channel_id_for_topic``.
* push ingest (``ingest_pushed_videos``, in-memory sqlite): catalogs genuinely-new
  videos via the normal poll path, is idempotent for duplicate pushes, defers a
  channel with no initial poll, and never touches the adaptive poll schedule.
* subscribe lifecycle (``sync_subscriptions`` / ``subscribe_channel``): posts the
  right hub params for due UC channels only, stamps the lease, and no-ops when
  disabled.
* callback router: GET echoes ``hub.challenge`` for a tracked topic (else 404),
  POST verifies the HMAC (valid -> dispatch + 204, bad/absent -> 404), and the
  whole router 404s when WebSub is disabled.
"""

import hashlib
import hmac
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.websub as websub_service
import app.utils.websub as websub_util
from app.config import settings
from app.database import async_session_factory
from app.models import Base
from app.models.channel import Channel
from app.models.subscription import UserSubscription
from app.models.video import Video
from app.services import channel_poller
from app.services.websub import (
    channel_id_for_topic,
    parse_push,
    subscribe_channel,
    sync_subscriptions,
    topic_url,
)
from app.utils.time import utcnow_naive
from app.utils.websub import verify_signature, websub_secret
from tests.helpers import seed_channel, seed_subscription

_UC_ID = "UCpushchannel00000000000"

# A realistic YouTube WebSub push: one new upload, channel id carried per-entry
# (as YouTube actually sends it) rather than at the feed level.
PUSH_BODY = f"""<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns="http://www.w3.org/2005/Atom">
  <link rel="hub" href="https://pubsubhubbub.appspot.com"/>
  <link rel="self"
    href="https://www.youtube.com/xml/feeds/videos.xml?channel_id={_UC_ID}"/>
  <title>YouTube video feed</title>
  <updated>2026-06-28T19:05:24+00:00</updated>
  <entry>
    <id>yt:video:PUSHVIDEO001</id>
    <yt:videoId>PUSHVIDEO001</yt:videoId>
    <yt:channelId>{_UC_ID}</yt:channelId>
    <title>Pushed Upload</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=PUSHVIDEO001"/>
    <author><name>Push Channel</name></author>
    <published>2026-06-28T19:00:00+00:00</published>
    <updated>2026-06-28T19:05:00+00:00</updated>
  </entry>
</feed>
""".encode()


# --- pure parsing helpers --------------------------------------------------


def test_parse_push_extracts_channel_and_video_ids():
    parsed = parse_push(PUSH_BODY)
    assert parsed["channel_id"] == _UC_ID
    assert parsed["video_ids"] == ["PUSHVIDEO001"]


def test_parse_push_prefers_feed_level_channel_id():
    body = f"""<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns="http://www.w3.org/2005/Atom">
  <yt:channelId>{_UC_ID}</yt:channelId>
  <entry>
    <yt:videoId>AAA00000001</yt:videoId>
    <title>One</title>
  </entry>
  <entry>
    <yt:videoId>BBB00000002</yt:videoId>
    <title>Two</title>
  </entry>
</feed>
""".encode()
    parsed = parse_push(body)
    assert parsed["channel_id"] == _UC_ID
    assert parsed["video_ids"] == ["AAA00000001", "BBB00000002"]


def test_parse_push_ignores_deletion_tombstone():
    # A deletion notification carries no <yt:videoId>; nothing to catalog.
    body = b"""<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:at="http://purl.org/atompub/tombstones/1.0"
      xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns="http://www.w3.org/2005/Atom">
  <at:deleted-entry ref="yt:video:GONE0000001" when="2026-06-28T19:00:00+00:00">
    <link href="https://www.youtube.com/watch?v=GONE0000001"/>
  </at:deleted-entry>
</feed>
"""
    parsed = parse_push(body)
    assert parsed["video_ids"] == []


def test_parse_push_unparseable_body_is_empty():
    parsed = parse_push(b"<<not xml>>")
    assert parsed == {"channel_id": None, "video_ids": []}


def test_channel_id_for_topic_roundtrip_and_garbage():
    assert channel_id_for_topic(topic_url(_UC_ID)) == _UC_ID
    assert channel_id_for_topic("https://example.com/feed") is None
    assert channel_id_for_topic(None) is None


def test_verify_signature_sha1_and_sha256(monkeypatch):
    monkeypatch.setattr(websub_util, "websub_secret", lambda: "secret-xyz")
    body = b"hello-body"
    sha1 = "sha1=" + hmac.new(b"secret-xyz", body, hashlib.sha1).hexdigest()
    sha256 = "sha256=" + hmac.new(b"secret-xyz", body, hashlib.sha256).hexdigest()
    assert verify_signature(body, sha1) is True
    assert verify_signature(body, sha256) is True
    # Wrong secret, tampered body, missing/garbage headers all reject.
    assert verify_signature(b"tampered", sha1) is False
    assert verify_signature(body, "sha1=deadbeef") is False
    assert verify_signature(body, "md5=" + "0" * 32) is False
    assert verify_signature(body, None) is False
    assert verify_signature(body, "garbage") is False


# --- push ingest (in-memory sqlite) ----------------------------------------


def _mem_db_with_channel(*user_ids, initial_poll_done=True, **channel_overrides):
    """In-memory poller DB with one UC channel + subscribers (FKs off)."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    channel = Channel(
        id="ch-1",
        youtube_channel_id=_UC_ID,
        name="Push Channel",
        slug="push-channel",
        last_checked_at=utcnow_naive() if initial_poll_done else None,
    )
    for key, value in channel_overrides.items():
        setattr(channel, key, value)
    db.add(channel)
    for uid in user_ids:
        db.add(UserSubscription(user_id=uid, channel_id=channel.id))
    db.commit()
    return db, channel


def test_ingest_catalogs_new_video_and_emits(monkeypatch):
    db, channel = _mem_db_with_channel("u1")
    meta_calls = []

    def fake_meta(ids):
        meta_calls.append(list(ids))
        return [
            {"youtube_video_id": v, "title": f"T-{v}", "duration_seconds": 0}
            for v in ids
        ]

    monkeypatch.setattr(channel_poller, "fetch_videos_metadata", fake_meta)
    publish_mock = MagicMock()
    monkeypatch.setattr(channel_poller, "publish_new_episode", publish_mock)
    monkeypatch.setattr(channel_poller, "send_to_users", MagicMock())

    result = channel_poller.ingest_pushed_videos("ch-1", ["PUSHVIDEO001"], db)

    assert len(result["cataloged_ids"]) == 1
    assert meta_calls == [["PUSHVIDEO001"]]
    rows = db.execute(select(Video.youtube_video_id)).scalars().all()
    assert rows == ["PUSHVIDEO001"]
    # new_episode emitted for the (1 subscriber x 1 new video).
    assert publish_mock.call_count == 1
    db.close()


def test_ingest_is_idempotent_for_duplicate_push(monkeypatch):
    db, channel = _mem_db_with_channel("u1")

    def fake_meta(ids):
        return [{"youtube_video_id": v, "title": v, "duration_seconds": 0} for v in ids]

    meta_mock = MagicMock(side_effect=fake_meta)
    monkeypatch.setattr(channel_poller, "fetch_videos_metadata", meta_mock)
    publish_mock = MagicMock()
    monkeypatch.setattr(channel_poller, "publish_new_episode", publish_mock)
    monkeypatch.setattr(channel_poller, "send_to_users", MagicMock())

    first = channel_poller.ingest_pushed_videos("ch-1", ["PUSHVIDEO001"], db)
    second = channel_poller.ingest_pushed_videos("ch-1", ["PUSHVIDEO001"], db)

    assert len(first["cataloged_ids"]) == 1
    assert second["cataloged_ids"] == []
    # The already-known id does no yt-dlp work the second time, and emits nothing.
    assert meta_mock.call_count == 1
    assert publish_mock.call_count == 1
    rows = (
        db.execute(select(Video).where(Video.youtube_video_id == "PUSHVIDEO001"))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    db.close()


def test_ingest_defers_when_no_initial_poll(monkeypatch):
    db, channel = _mem_db_with_channel("u1", initial_poll_done=False)
    meta_mock = MagicMock()
    monkeypatch.setattr(channel_poller, "fetch_videos_metadata", meta_mock)

    result = channel_poller.ingest_pushed_videos("ch-1", ["PUSHVIDEO001"], db)

    assert result == {"cataloged_ids": [], "auto_download_ids": []}
    meta_mock.assert_not_called()
    assert db.execute(select(Video)).scalars().all() == []
    db.close()


def test_ingest_unknown_channel_is_noop(monkeypatch):
    db, channel = _mem_db_with_channel("u1")
    meta_mock = MagicMock()
    monkeypatch.setattr(channel_poller, "fetch_videos_metadata", meta_mock)

    result = channel_poller.ingest_pushed_videos("does-not-exist", ["X"], db)

    assert result == {"cataloged_ids": [], "auto_download_ids": []}
    meta_mock.assert_not_called()
    db.close()


def test_ingest_leaves_poll_schedule_untouched(monkeypatch):
    """WebSub is additive: it must not move the RSS poller's adaptive cadence."""
    next_poll = utcnow_naive() + timedelta(minutes=99)
    last_checked = utcnow_naive() - timedelta(hours=3)
    db, channel = _mem_db_with_channel(
        "u1",
        next_poll_at=next_poll,
        poll_interval_minutes=120,
        last_checked_at=last_checked,
    )

    monkeypatch.setattr(
        channel_poller,
        "fetch_videos_metadata",
        lambda ids: [
            {"youtube_video_id": v, "title": v, "duration_seconds": 0} for v in ids
        ],
    )
    monkeypatch.setattr(channel_poller, "publish_new_episode", MagicMock())
    monkeypatch.setattr(channel_poller, "send_to_users", MagicMock())

    channel_poller.ingest_pushed_videos("ch-1", ["PUSHVIDEO001"], db)

    db.refresh(channel)
    assert channel.next_poll_at == next_poll
    assert channel.poll_interval_minutes == 120
    assert channel.last_checked_at == last_checked
    db.close()


# --- subscribe lifecycle ----------------------------------------------------


@pytest.fixture
def enabled_websub(monkeypatch):
    """Configure a callback URL so WebSub is enabled for the test."""
    monkeypatch.setattr(
        settings, "websub_callback_url", "https://nf.example.com/api/websub/callback"
    )
    return settings.websub_callback_url


def _sub_mem_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_subscribe_channel_posts_expected_hub_params(monkeypatch, enabled_websub):
    # subscribe_channel binds websub_secret in its own module namespace.
    monkeypatch.setattr(websub_service, "websub_secret", lambda: "the-secret")
    captured = {}

    class _Resp:
        status_code = 202

    def fake_post(url, data=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        return _Resp()

    monkeypatch.setattr(websub_service.httpx, "post", fake_post)

    channel = Channel(id="c1", youtube_channel_id=_UC_ID, name="n", slug="s")
    assert subscribe_channel(channel) is True
    assert captured["url"] == settings.websub_hub_url
    assert captured["data"]["hub.callback"] == enabled_websub
    assert captured["data"]["hub.topic"] == topic_url(_UC_ID)
    assert captured["data"]["hub.mode"] == "subscribe"
    assert captured["data"]["hub.verify"] == "async"
    assert captured["data"]["hub.secret"] == "the-secret"
    assert captured["data"]["hub.lease_seconds"] == str(settings.websub_lease_seconds)


def test_subscribe_channel_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(settings, "websub_callback_url", "")
    post_mock = MagicMock()
    monkeypatch.setattr(websub_service.httpx, "post", post_mock)
    channel = Channel(id="c1", youtube_channel_id=_UC_ID, name="n", slug="s")
    assert subscribe_channel(channel) is False
    post_mock.assert_not_called()


def test_subscribe_channel_non_2xx_returns_false(monkeypatch, enabled_websub):
    class _Resp:
        status_code = 500

    monkeypatch.setattr(websub_service.httpx, "post", lambda *a, **k: _Resp())
    channel = Channel(id="c1", youtube_channel_id=_UC_ID, name="n", slug="s")
    assert subscribe_channel(channel) is False


def test_sync_subscriptions_disabled_no_ops(monkeypatch):
    monkeypatch.setattr(settings, "websub_callback_url", "")
    post_mock = MagicMock()
    monkeypatch.setattr(websub_service.httpx, "post", post_mock)
    db = _sub_mem_db()()
    db.add(Channel(id="c1", youtube_channel_id=_UC_ID, name="n", slug="s"))
    db.add(UserSubscription(user_id="u1", channel_id="c1"))
    db.commit()

    result = sync_subscriptions(db)

    assert result == {"status": "disabled", "subscribed": 0}
    post_mock.assert_not_called()
    db.close()


def test_sync_subscriptions_subscribes_only_due_uc_channels(
    monkeypatch, enabled_websub
):
    posted_topics = []

    class _Resp:
        status_code = 202

    def fake_post(url, data=None, timeout=None):
        posted_topics.append(data["hub.topic"])
        return _Resp()

    monkeypatch.setattr(websub_service.httpx, "post", fake_post)

    session_local = _sub_mem_db()
    db = session_local()
    now = utcnow_naive()
    # Due: never subscribed (NULL lease).
    db.add(
        Channel(
            id="due-null",
            youtube_channel_id="UCdue0000000000000000a",
            name="a",
            slug="a",
        )
    )
    db.add(UserSubscription(user_id="u1", channel_id="due-null"))
    # Due: lease near expiry (inside the renewal window).
    db.add(
        Channel(
            id="due-soon",
            youtube_channel_id="UCdue0000000000000000b",
            name="b",
            slug="b",
            websub_expires_at=now + timedelta(hours=1),
        )
    )
    db.add(UserSubscription(user_id="u1", channel_id="due-soon"))
    # Not due: lease comfortably in the future.
    db.add(
        Channel(
            id="fresh",
            youtube_channel_id="UCfresh000000000000000c",
            name="c",
            slug="c",
            websub_expires_at=now + timedelta(days=4),
        )
    )
    db.add(UserSubscription(user_id="u1", channel_id="fresh"))
    # Skipped: non-UC id (feed not addressable).
    db.add(Channel(id="handle", youtube_channel_id="@handle", name="d", slug="d"))
    db.add(UserSubscription(user_id="u1", channel_id="handle"))
    # Skipped: UC but no subscriber (not tracked).
    db.add(
        Channel(
            id="nosub", youtube_channel_id="UCnosub00000000000000e", name="e", slug="e"
        )
    )
    db.commit()

    result = sync_subscriptions(db)

    assert result["status"] == "ok"
    assert result["subscribed"] == 2
    assert set(posted_topics) == {
        topic_url("UCdue0000000000000000a"),
        topic_url("UCdue0000000000000000b"),
    }
    # The two due channels got an optimistic lease stamp; others are unchanged.
    db2 = session_local()
    by_id = {c.id: c for c in db2.execute(select(Channel)).scalars().all()}
    assert by_id["due-null"].websub_expires_at is not None
    assert by_id["due-soon"].websub_expires_at > now + timedelta(days=1)
    assert by_id["fresh"].websub_expires_at == now + timedelta(days=4)
    assert by_id["handle"].websub_expires_at is None
    db2.close()
    db.close()


def test_sync_subscriptions_failed_post_leaves_lease_due(monkeypatch, enabled_websub):
    """A hub failure must not stamp a lease, so the channel retries next beat."""

    class _Resp:
        status_code = 503

    monkeypatch.setattr(websub_service.httpx, "post", lambda *a, **k: _Resp())
    session_local = _sub_mem_db()
    db = session_local()
    db.add(Channel(id="c1", youtube_channel_id=_UC_ID, name="n", slug="s"))
    db.add(UserSubscription(user_id="u1", channel_id="c1"))
    db.commit()

    result = sync_subscriptions(db)

    assert result["subscribed"] == 0
    db2 = session_local()
    ch = db2.get(Channel, "c1")
    assert ch.websub_expires_at is None
    db2.close()
    db.close()


# --- callback router (async client) ----------------------------------------


def _sign(body: bytes) -> str:
    return "sha1=" + hmac.new(websub_secret().encode(), body, hashlib.sha1).hexdigest()


@pytest.mark.asyncio
async def test_callback_get_echoes_challenge_for_tracked_topic(
    client, make_user, monkeypatch
):
    monkeypatch.setattr(
        settings, "websub_callback_url", "https://nf.example.com/api/websub/callback"
    )
    user, _ = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db, youtube_channel_id=_UC_ID)
        await seed_subscription(db, user["id"], channel.id)

    resp = await client.get(
        "/api/websub/callback",
        params={
            "hub.mode": "subscribe",
            "hub.topic": topic_url(_UC_ID),
            "hub.challenge": "challenge-token-123",
            "hub.lease_seconds": "432000",
        },
    )

    assert resp.status_code == 200
    assert resp.text == "challenge-token-123"
    assert resp.headers["content-type"].startswith("text/plain")


@pytest.mark.asyncio
async def test_callback_get_404_for_untracked_topic(client, make_user, monkeypatch):
    monkeypatch.setattr(
        settings, "websub_callback_url", "https://nf.example.com/api/websub/callback"
    )
    await make_user()
    resp = await client.get(
        "/api/websub/callback",
        params={
            "hub.mode": "subscribe",
            "hub.topic": topic_url("UCnottracked0000000000"),
            "hub.challenge": "x",
        },
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_callback_get_404_when_disabled(client, make_user, monkeypatch):
    monkeypatch.setattr(settings, "websub_callback_url", "")
    user, _ = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db, youtube_channel_id=_UC_ID)
        await seed_subscription(db, user["id"], channel.id)

    resp = await client.get(
        "/api/websub/callback",
        params={
            "hub.mode": "subscribe",
            "hub.topic": topic_url(_UC_ID),
            "hub.challenge": "x",
        },
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_callback_post_valid_signature_dispatches_ingest(
    client, make_user, monkeypatch
):
    monkeypatch.setattr(
        settings, "websub_callback_url", "https://nf.example.com/api/websub/callback"
    )
    user, _ = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db, youtube_channel_id=_UC_ID)
        await seed_subscription(db, user["id"], channel.id)
        channel_id = channel.id

    delay_mock = MagicMock()
    monkeypatch.setattr("app.api.websub.ingest_websub_push_task.delay", delay_mock)

    resp = await client.post(
        "/api/websub/callback",
        content=PUSH_BODY,
        headers={"X-Hub-Signature": _sign(PUSH_BODY)},
    )

    assert resp.status_code == 204
    delay_mock.assert_called_once_with(channel_id, ["PUSHVIDEO001"])


@pytest.mark.asyncio
async def test_callback_post_bad_signature_rejected(client, make_user, monkeypatch):
    monkeypatch.setattr(
        settings, "websub_callback_url", "https://nf.example.com/api/websub/callback"
    )
    user, _ = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db, youtube_channel_id=_UC_ID)
        await seed_subscription(db, user["id"], channel.id)

    delay_mock = MagicMock()
    monkeypatch.setattr("app.api.websub.ingest_websub_push_task.delay", delay_mock)

    resp = await client.post(
        "/api/websub/callback",
        content=PUSH_BODY,
        headers={"X-Hub-Signature": "sha1=deadbeef"},
    )

    assert resp.status_code == 404
    delay_mock.assert_not_called()


@pytest.mark.asyncio
async def test_callback_post_missing_signature_rejected(client, make_user, monkeypatch):
    monkeypatch.setattr(
        settings, "websub_callback_url", "https://nf.example.com/api/websub/callback"
    )
    await make_user()
    delay_mock = MagicMock()
    monkeypatch.setattr("app.api.websub.ingest_websub_push_task.delay", delay_mock)

    resp = await client.post("/api/websub/callback", content=PUSH_BODY)

    assert resp.status_code == 404
    delay_mock.assert_not_called()


@pytest.mark.asyncio
async def test_callback_post_untracked_channel_accepts_without_dispatch(
    client, make_user, monkeypatch
):
    """A signed push for a channel we don't track is accepted (204) but ignored."""
    monkeypatch.setattr(
        settings, "websub_callback_url", "https://nf.example.com/api/websub/callback"
    )
    await make_user()  # no channel/subscription seeded
    delay_mock = MagicMock()
    monkeypatch.setattr("app.api.websub.ingest_websub_push_task.delay", delay_mock)

    resp = await client.post(
        "/api/websub/callback",
        content=PUSH_BODY,
        headers={"X-Hub-Signature": _sign(PUSH_BODY)},
    )

    assert resp.status_code == 204
    delay_mock.assert_not_called()


@pytest.mark.asyncio
async def test_callback_post_404_when_disabled(client, make_user, monkeypatch):
    monkeypatch.setattr(settings, "websub_callback_url", "")
    await make_user()
    delay_mock = MagicMock()
    monkeypatch.setattr("app.api.websub.ingest_websub_push_task.delay", delay_mock)

    resp = await client.post(
        "/api/websub/callback",
        content=PUSH_BODY,
        headers={"X-Hub-Signature": _sign(PUSH_BODY)},
    )

    assert resp.status_code == 404
    delay_mock.assert_not_called()
