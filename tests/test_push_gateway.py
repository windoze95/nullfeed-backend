"""Tests for the push gateway integration (#33).

Everything mocks ``httpx`` at ``push_gateway.httpx.request`` — there is no real
network. Covered: auto-enroll persists + reuses the key, the device-register
forwards user id + token + topic, a new episode triggers a per-subscriber send
with the right ``to.user_ids`` + ``data.video_id``, push DISABLED no-ops both the
endpoints and the poller (no call-out), and a gateway error during a send never
breaks the poll.
"""

from unittest.mock import MagicMock

import httpx
import pytest

import app.services.push_gateway as push_gateway
from app.config import settings


# --- test doubles ----------------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for ``httpx.Response`` (status + optional JSON body)."""

    def __init__(self, status_code: int = 200, json_data: dict | None = None):
        self.status_code = status_code
        self._json = {} if json_data is None else json_data
        self.text = ""

    def json(self) -> dict:
        return self._json


def _fake_request(record: list, handler):
    """Build an ``httpx.request`` replacement that records and routes calls.

    ``handler(method, url)`` returns a :class:`_FakeResponse`, or an ``Exception``
    instance to be raised (to simulate a transport error / non-2xx).
    """

    def fake(method, url, *, json=None, headers=None, timeout=None):
        record.append(
            {"method": method, "url": url, "json": json, "headers": headers or {}}
        )
        result = handler(method, url)
        if isinstance(result, Exception):
            raise result
        return result

    return fake


def _no_network(*_args, **_kwargs):
    raise AssertionError("unexpected push gateway network call")


def _enable_push(monkeypatch, tmp_path, *, api_key: str = "pgk_explicit") -> None:
    """Enable push with an explicit (non-persisted) key and an isolated config dir."""
    monkeypatch.setattr(settings, "push_gateway_url", "https://gw.test")
    monkeypatch.setattr(settings, "push_api_key", api_key)
    monkeypatch.setattr(settings, "push_enroll_token", "")
    monkeypatch.setattr(settings, "config_path", str(tmp_path))
    push_gateway._reset_cache()


def _poller_db(*user_ids: str, initial_poll_done: bool = True):
    """In-memory poller DB with one channel + the given subscribers."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models import Base
    from app.models.channel import Channel
    from app.models.subscription import UserSubscription
    from app.utils.time import utcnow_naive

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    channel = Channel(
        id="ch-1",
        youtube_channel_id="@testchannel",
        name="Test Channel",
        slug="testchannel",
        last_checked_at=utcnow_naive() if initial_poll_done else None,
    )
    db.add(channel)
    for uid in user_ids:
        db.add(UserSubscription(user_id=uid, channel_id=channel.id))
    db.commit()
    return db, channel


# --- auto-enroll persistence ----------------------------------------------


def test_auto_enroll_persists_and_reuses_key(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "push_gateway_url", "https://gw.test")
    monkeypatch.setattr(settings, "push_api_key", "")  # force auto-enroll
    monkeypatch.setattr(settings, "push_enroll_token", "")
    monkeypatch.setattr(settings, "config_path", str(tmp_path))
    push_gateway._reset_cache()

    record: list = []

    def handler(method, url):
        if url.endswith("/v1/enroll"):
            return _FakeResponse(201, {"api_key": "pgk_enrolled", "tenant_id": "t-1"})
        return _FakeResponse(200, {"id": "d", "created": True})

    monkeypatch.setattr(push_gateway.httpx, "request", _fake_request(record, handler))

    key = push_gateway._resolve_api_key()
    assert key == "pgk_enrolled"

    # Persisted as a 0600 file under config_path.
    key_file = tmp_path / "push_api_key"
    assert key_file.read_text().strip() == "pgk_enrolled"
    assert (key_file.stat().st_mode & 0o777) == 0o600

    enroll_calls = [c for c in record if c["url"].endswith("/v1/enroll")]
    assert len(enroll_calls) == 1
    assert enroll_calls[0]["json"] == {"name": "NullFeed"}
    assert "Authorization" not in enroll_calls[0]["headers"]  # enroll is unauth

    # A fresh process (cache dropped) reuses the persisted key — no re-enroll.
    push_gateway._reset_cache()
    assert push_gateway._resolve_api_key() == "pgk_enrolled"
    assert len([c for c in record if c["url"].endswith("/v1/enroll")]) == 1


def test_auto_enroll_sends_enroll_token_when_gated(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "push_gateway_url", "https://gw.test")
    monkeypatch.setattr(settings, "push_api_key", "")
    monkeypatch.setattr(settings, "push_enroll_token", "secret-enroll")
    monkeypatch.setattr(settings, "config_path", str(tmp_path))
    push_gateway._reset_cache()

    record: list = []
    monkeypatch.setattr(
        push_gateway.httpx,
        "request",
        _fake_request(record, lambda m, u: _FakeResponse(201, {"api_key": "pgk_g"})),
    )

    assert push_gateway._resolve_api_key() == "pgk_g"
    enroll_call = next(c for c in record if c["url"].endswith("/v1/enroll"))
    assert enroll_call["headers"].get("X-Enroll-Token") == "secret-enroll"


# --- device registration ---------------------------------------------------


def test_register_forwards_user_token_topic(monkeypatch, tmp_path):
    _enable_push(monkeypatch, tmp_path)
    record: list = []
    monkeypatch.setattr(
        push_gateway.httpx,
        "request",
        _fake_request(
            record, lambda m, u: _FakeResponse(200, {"id": "d", "created": True})
        ),
    )

    assert (
        push_gateway.register_device("user-42", "apns-tok", device_id="dev-1") is True
    )

    assert len(record) == 1
    call = record[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/v1/devices")
    assert call["json"] == {
        "user_id": "user-42",
        "platform": "ios",
        "token": "apns-tok",
        "topic": "codes.julian.nullfeed",
        "device_id": "dev-1",
    }
    assert call["headers"]["Authorization"] == "Bearer pgk_explicit"
    # An explicit key is never persisted.
    assert not (tmp_path / "push_api_key").exists()


def test_unregister_by_device_id(monkeypatch, tmp_path):
    _enable_push(monkeypatch, tmp_path)
    record: list = []
    monkeypatch.setattr(
        push_gateway.httpx,
        "request",
        _fake_request(record, lambda m, u: _FakeResponse(204)),
    )

    assert push_gateway.unregister_device(device_id="dev-1") is True
    assert record[0]["method"] == "DELETE"
    assert record[0]["url"].endswith("/v1/devices")
    assert record[0]["json"] == {"device_id": "dev-1"}


def test_unregister_without_identifier_is_noop(monkeypatch, tmp_path):
    _enable_push(monkeypatch, tmp_path)
    monkeypatch.setattr(push_gateway.httpx, "request", _no_network)
    assert push_gateway.unregister_device() is False  # no network call


# --- notification send -----------------------------------------------------


def test_send_to_users_targets_user_ids_with_data(monkeypatch, tmp_path):
    _enable_push(monkeypatch, tmp_path)
    record: list = []
    monkeypatch.setattr(
        push_gateway.httpx,
        "request",
        _fake_request(
            record,
            lambda m, u: _FakeResponse(200, {"sent": 1, "failed": 0, "results": []}),
        ),
    )

    ok = push_gateway.send_to_users(
        ["u1"], "Chan", "Vid Title", {"type": "new_episode", "video_id": "v1"}
    )
    assert ok is True

    body = record[0]["json"]
    assert record[0]["url"].endswith("/v1/notifications")
    assert body["to"] == {"user_ids": ["u1"]}
    assert body["notification"] == {"title": "Chan", "body": "Vid Title"}
    assert body["data"] == {"type": "new_episode", "video_id": "v1"}
    assert body["apns"] == {"topic": "codes.julian.nullfeed"}


def test_send_error_is_swallowed(monkeypatch, tmp_path):
    _enable_push(monkeypatch, tmp_path)
    monkeypatch.setattr(
        push_gateway.httpx,
        "request",
        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")),
    )
    # Best-effort: a transport error returns False, never raises.
    assert push_gateway.send_to_users(["u1"], "t", "b", {}) is False


# --- push disabled ---------------------------------------------------------


def test_disabled_functions_make_no_calls(monkeypatch):
    monkeypatch.setattr(settings, "push_gateway_url", "")
    push_gateway._reset_cache()
    monkeypatch.setattr(push_gateway.httpx, "request", _no_network)

    assert push_gateway.push_enabled() is False
    assert push_gateway._resolve_api_key() is None
    assert push_gateway.register_device("u1", "tok") is False
    assert push_gateway.unregister_device(device_id="d") is False
    assert push_gateway.send_to_users(["u1"], "t", "b", {}) is False


# --- poller integration ----------------------------------------------------


def test_new_episode_triggers_push_per_subscriber(monkeypatch, tmp_path):
    from app.services import channel_poller

    _enable_push(monkeypatch, tmp_path)
    db, channel = _poller_db("u1", "u2", initial_poll_done=True)
    feed = {"videos": [{"youtube_video_id": "vid00000001", "title": "New One"}]}
    monkeypatch.setattr(channel_poller, "fetch_channel_videos", lambda _: feed)
    monkeypatch.setattr(channel_poller, "publish_new_episode", MagicMock())

    record: list = []
    monkeypatch.setattr(
        push_gateway.httpx,
        "request",
        _fake_request(
            record,
            lambda m, u: _FakeResponse(200, {"sent": 1, "failed": 0, "results": []}),
        ),
    )

    result = channel_poller.poll_single_channel(channel.id, db)
    video_id = result["cataloged_ids"][0]

    notifs = [c for c in record if c["url"].endswith("/v1/notifications")]
    assert len(notifs) == 2  # one per subscriber x 1 new video
    targets = set()
    for c in notifs:
        body = c["json"]
        assert body["data"] == {"type": "new_episode", "video_id": video_id}
        assert body["notification"]["title"] == "Test Channel"
        assert body["notification"]["body"] == "New One"
        (uid,) = body["to"]["user_ids"]
        targets.add(uid)
    assert targets == {"u1", "u2"}
    db.close()


def test_poller_does_not_call_out_when_push_disabled(monkeypatch):
    from app.services import channel_poller

    monkeypatch.setattr(settings, "push_gateway_url", "")
    push_gateway._reset_cache()

    db, channel = _poller_db("u1", initial_poll_done=True)
    feed = {"videos": [{"youtube_video_id": "vid00000001", "title": "New One"}]}
    monkeypatch.setattr(channel_poller, "fetch_channel_videos", lambda _: feed)
    monkeypatch.setattr(channel_poller, "publish_new_episode", MagicMock())

    calls: list = []

    def counting(*a, **k):
        calls.append(1)
        return _FakeResponse(200, {})

    monkeypatch.setattr(push_gateway.httpx, "request", counting)

    result = channel_poller.poll_single_channel(channel.id, db)
    assert len(result["cataloged_ids"]) == 1  # poll still catalogs
    assert calls == []  # never reached the gateway
    db.close()


def test_gateway_error_during_send_does_not_break_poll(monkeypatch, tmp_path):
    from app.services import channel_poller

    _enable_push(monkeypatch, tmp_path)
    db, channel = _poller_db("u1", initial_poll_done=True)
    feed = {"videos": [{"youtube_video_id": "vid00000001", "title": "New One"}]}
    monkeypatch.setattr(channel_poller, "fetch_channel_videos", lambda _: feed)
    ws_mock = MagicMock()
    monkeypatch.setattr(channel_poller, "publish_new_episode", ws_mock)

    def boom(method, url, *, json=None, headers=None, timeout=None):
        raise httpx.ConnectError("gateway down")

    monkeypatch.setattr(push_gateway.httpx, "request", boom)

    result = channel_poller.poll_single_channel(channel.id, db)
    assert len(result["cataloged_ids"]) == 1  # poll succeeded despite push failure
    ws_mock.assert_called_once()  # WS publish still happened
    db.close()


# --- endpoints -------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_endpoint_forwards_user_and_topic(
    client, make_user, monkeypatch, tmp_path
):
    _enable_push(monkeypatch, tmp_path)
    record: list = []
    monkeypatch.setattr(
        push_gateway.httpx,
        "request",
        _fake_request(
            record, lambda m, u: _FakeResponse(200, {"id": "d1", "created": True})
        ),
    )

    profile, headers = await make_user()
    resp = await client.post(
        "/api/push/register",
        headers=headers,
        json={"device_token": "apns-tok", "device_id": "dev-1"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enabled": True, "registered": True}

    devices = [c for c in record if c["url"].endswith("/v1/devices")]
    assert len(devices) == 1
    sent = devices[0]["json"]
    assert sent["user_id"] == profile["id"]
    assert sent["token"] == "apns-tok"
    assert sent["device_id"] == "dev-1"
    assert sent["topic"] == "codes.julian.nullfeed"
    assert devices[0]["headers"]["Authorization"] == "Bearer pgk_explicit"


@pytest.mark.asyncio
async def test_unregister_endpoint_forwards(client, make_user, monkeypatch, tmp_path):
    _enable_push(monkeypatch, tmp_path)
    record: list = []
    monkeypatch.setattr(
        push_gateway.httpx,
        "request",
        _fake_request(record, lambda m, u: _FakeResponse(204)),
    )

    _, headers = await make_user()
    resp = await client.request(
        "DELETE", "/api/push/register", headers=headers, json={"device_id": "dev-1"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enabled": True, "unregistered": True}

    devices = [c for c in record if c["url"].endswith("/v1/devices")]
    assert devices[0]["method"] == "DELETE"
    assert devices[0]["json"] == {"device_id": "dev-1"}


@pytest.mark.asyncio
async def test_register_endpoint_requires_auth(client, monkeypatch, tmp_path):
    _enable_push(monkeypatch, tmp_path)
    monkeypatch.setattr(push_gateway.httpx, "request", _no_network)
    resp = await client.post("/api/push/register", json={"device_token": "t"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_register_endpoint_disabled_returns_disabled(
    client, make_user, monkeypatch
):
    monkeypatch.setattr(settings, "push_gateway_url", "")
    push_gateway._reset_cache()
    monkeypatch.setattr(push_gateway.httpx, "request", _no_network)

    _, headers = await make_user()
    resp = await client.post(
        "/api/push/register", headers=headers, json={"device_token": "tok"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False}
