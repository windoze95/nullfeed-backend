"""Unit tests for the in-process PIN brute-force throttle.

These exercise the throttle helpers directly (no HTTP/event loop) so the clock
can be driven deterministically via a patched ``time.monotonic``. The autouse
``_reset_in_memory_state`` fixture in conftest clears the throttle map between
tests.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.auth as auth_api


def _fake_request(host: str | None = "198.51.100.4", xff: str | None = None):
    """A minimal stand-in for starlette's Request (only what _client_ip reads)."""
    headers = {"x-forwarded-for": xff} if xff is not None else {}
    client = SimpleNamespace(host=host) if host is not None else None
    return SimpleNamespace(headers=headers, client=client)


def test_client_ip_uses_socket_peer_by_default(monkeypatch):
    monkeypatch.setattr(auth_api.settings, "trust_proxy_headers", False)
    req = _fake_request(host="10.0.0.5", xff="1.1.1.1, 2.2.2.2")
    # XFF is ignored unless a proxy is trusted, so a client can't forge its key.
    assert auth_api._client_ip(req) == "10.0.0.5"


def test_client_ip_trusts_rightmost_xff_when_enabled(monkeypatch):
    monkeypatch.setattr(auth_api.settings, "trust_proxy_headers", True)
    # The rightmost entry is what the trusted proxy observed; the leftmost ones
    # are attacker-controllable and must never be used.
    req = _fake_request(host="10.0.0.5", xff="1.1.1.1, 2.2.2.2, 3.3.3.3")
    assert auth_api._client_ip(req) == "3.3.3.3"


def test_client_ip_falls_back_when_no_peer(monkeypatch):
    monkeypatch.setattr(auth_api.settings, "trust_proxy_headers", False)
    assert auth_api._client_ip(_fake_request(host=None)) == "unknown"


def test_pin_rate_key_combines_ip_and_user():
    req = _fake_request(host="10.0.0.5")
    assert auth_api._pin_rate_key(req, "user-123") == "10.0.0.5|user-123"


def test_backoff_is_exponential_and_per_key(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(auth_api.time, "monotonic", lambda: clock["t"])

    victim = "10.0.0.1|user-x"
    other_ip = "10.0.0.2|user-x"  # same user, different source IP

    # Failures below the threshold never lock the key.
    for _ in range(auth_api._PIN_MAX_FAILURES - 1):
        auth_api._record_pin_failure(victim)
        auth_api._check_pin_rate_limit(victim)  # must not raise

    # The threshold failure arms the base lockout window.
    auth_api._record_pin_failure(victim)
    with pytest.raises(HTTPException) as first:
        auth_api._check_pin_rate_limit(victim)
    assert first.value.status_code == 429
    base = int(first.value.headers["Retry-After"])
    assert 0 < base <= auth_api._PIN_LOCKOUT_BASE_SECONDS

    # A different source IP for the same user is unaffected by the failures.
    auth_api._check_pin_rate_limit(other_ip)  # must not raise

    # After the window elapses the lock clears but the failure count is kept,
    # so the next failure escalates (roughly doubles) the backoff.
    clock["t"] += auth_api._PIN_LOCKOUT_BASE_SECONDS + 1
    auth_api._check_pin_rate_limit(victim)  # expired -> no raise
    auth_api._record_pin_failure(victim)
    with pytest.raises(HTTPException) as second:
        auth_api._check_pin_rate_limit(victim)
    assert int(second.value.headers["Retry-After"]) > base


def test_backoff_is_capped(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(auth_api.time, "monotonic", lambda: clock["t"])
    key = "10.0.0.9|user-z"
    # Drive far past the doubling range; the lockout must not exceed the cap.
    for _ in range(40):
        auth_api._record_pin_failure(key)
    with pytest.raises(HTTPException) as exc:
        auth_api._check_pin_rate_limit(key)
    assert int(exc.value.headers["Retry-After"]) <= auth_api._PIN_LOCKOUT_MAX_SECONDS


def test_correct_pin_reset_clears_backoff(monkeypatch):
    clock = {"t": 5000.0}
    monkeypatch.setattr(auth_api.time, "monotonic", lambda: clock["t"])
    key = "10.0.0.7|user-y"
    for _ in range(auth_api._PIN_MAX_FAILURES):
        auth_api._record_pin_failure(key)
    with pytest.raises(HTTPException):
        auth_api._check_pin_rate_limit(key)

    auth_api._reset_pin_throttle(key)
    auth_api._check_pin_rate_limit(key)  # cleared -> no raise


def test_idle_key_is_forgotten_after_reset_window(monkeypatch):
    clock = {"t": 100.0}
    monkeypatch.setattr(auth_api.time, "monotonic", lambda: clock["t"])
    key = "10.0.0.3|user-w"
    for _ in range(auth_api._PIN_MAX_FAILURES):
        auth_api._record_pin_failure(key)

    # Long after the lockout (and any further activity), the key is dropped so
    # an honest user gets a clean slate.
    clock["t"] += auth_api._PIN_FAILURE_RESET_SECONDS + 1
    auth_api._check_pin_rate_limit(key)  # no raise; also prunes the entry
    assert key not in auth_api._pin_throttle


def test_reset_for_user_clears_all_ips(monkeypatch):
    clock = {"t": 9000.0}
    monkeypatch.setattr(auth_api.time, "monotonic", lambda: clock["t"])
    victim_a = "10.0.0.1|victim"
    victim_b = "10.0.0.2|victim"
    bystander = "10.0.0.1|bystander"
    for key in (victim_a, victim_b, bystander):
        for _ in range(auth_api._PIN_MAX_FAILURES):
            auth_api._record_pin_failure(key)

    auth_api._reset_pin_throttle_for_user("victim")

    # Both of the victim's keys (across IPs) are cleared...
    auth_api._check_pin_rate_limit(victim_a)  # no raise
    auth_api._check_pin_rate_limit(victim_b)  # no raise
    # ...but an unrelated user's lockout is left intact.
    with pytest.raises(HTTPException):
        auth_api._check_pin_rate_limit(bystander)
