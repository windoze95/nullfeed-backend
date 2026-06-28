"""Best-effort integration with the public push gateway (push.julian.codes).

NullFeed relays APNs notifications through a shared, multi-tenant gateway so a
self-hoster needs no Apple push key of their own. This module speaks the
gateway's ``/v1`` HTTP API (Bearer per-tenant key) to register device tokens and
fan notifications out to a user's devices.

Three design rules shape everything here:

* **Best-effort.** Push is a nicety layered on top of the WebSocket live events,
  never a dependency of the core flows. A disabled gateway, an unreachable
  gateway, or a per-call error must NEVER raise into the poller or an endpoint:
  the public helpers catch everything, log, and return a simple ``bool``.
* **Zero-config auto-enrollment.** With no explicit key set, the backend
  self-enrolls as a tenant on first use and persists the issued ``pgk_`` key
  durably under ``settings.config_path`` (a 0600 file, claimed with the same
  atomic hard-link dance as :mod:`app.utils.tickets`) so it survives restarts
  and is shared by every worker on the same volume. An explicit
  ``settings.push_api_key`` wins and is never persisted.
* **Sync core, async edges.** The HTTP calls are plain synchronous ``httpx`` so
  the Celery poller (a sync context) can call them directly; FastAPI endpoints
  use the ``*_async`` wrappers, which run the sync call in a worker thread via
  :func:`asyncio.to_thread` so the event loop is never blocked.

The APNs topic / iOS bundle id is always :data:`PUSH_TOPIC`. Tenant keys may only
target ``user_ids`` (the gateway rejects raw device tokens for tenant keys), so
sends fan out by NullFeed user id.
"""

import asyncio
import logging
import os
from pathlib import Path
from threading import Lock

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Fixed APNs topic (the iOS bundle id) for NullFeed's tenant on the gateway.
PUSH_TOPIC = "codes.julian.nullfeed"

# Tenant label sent to the gateway on auto-enroll.
_ENROLL_NAME = "NullFeed"

# Filename of the auto-enrolled tenant key persisted under settings.config_path.
_API_KEY_FILENAME = "push_api_key"

# Network timeout (seconds) for every gateway call.
_TIMEOUT = 15.0

# Cache the resolved (persisted / auto-enrolled) key so the hot send path does
# not re-read the file on every call. An explicit settings key is never cached
# here (it already lives in settings, so tests can override it freely). The lock
# makes auto-enrollment single-flight within a process; the persisted file
# converges all processes on one key.
_cached_api_key: str | None = None
_key_lock = Lock()


class PushGatewayError(Exception):
    """Raised by the low-level HTTP helpers on a transport error or non-2xx."""


def push_enabled() -> bool:
    """True when a gateway URL is configured; everything no-ops when False."""
    return bool(settings.push_gateway_url)


def _gateway_url() -> str:
    return settings.push_gateway_url.rstrip("/")


# --- low-level HTTP --------------------------------------------------------


def _http(
    method: str,
    url: str,
    *,
    json: dict | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Issue one gateway request, raising :class:`PushGatewayError` on failure."""
    try:
        resp = httpx.request(method, url, json=json, headers=headers, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        raise PushGatewayError(f"{method} {url} transport error: {exc}") from exc
    if resp.status_code // 100 != 2:
        raise PushGatewayError(
            f"{method} {url} returned {resp.status_code}: {resp.text[:200]}"
        )
    return resp


# --- key resolution + persistence ------------------------------------------


def _key_path() -> Path:
    return Path(settings.config_path) / _API_KEY_FILENAME


def _read_persisted_key() -> str | None:
    try:
        existing = _key_path().read_text().strip()
        return existing or None
    except FileNotFoundError:
        return None


def _persist_key(api_key: str) -> str:
    """Persist the auto-enrolled key durably, atomically, shared across workers.

    Mirrors :func:`app.utils.tickets._load_or_create_persisted_secret`: write a
    private 0600 temp file then hard-link it into place (create-only, atomic).
    Whoever wins the link keeps its key; everyone racing reads the winner's, so
    all workers converge on a single key and the file is never half-written.
    """
    path = _key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    tmp.write_text(api_key)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    try:
        os.link(tmp, path)  # atomic create-only claim; raises if path exists
        return api_key
    except FileExistsError:
        return path.read_text().strip()
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def enroll() -> str:
    """Self-enroll NullFeed as a tenant and return the issued key (raises).

    Unauthenticated; sends ``X-Enroll-Token`` when ``settings.push_enroll_token``
    is set (required only for a gateway running in ``gated`` enrollment mode).
    """
    headers: dict[str, str] = {}
    if settings.push_enroll_token:
        headers["X-Enroll-Token"] = settings.push_enroll_token
    resp = _http(
        "POST",
        f"{_gateway_url()}/v1/enroll",
        json={"name": _ENROLL_NAME},
        headers=headers or None,
    )
    api_key = resp.json().get("api_key")
    if not api_key:
        raise PushGatewayError("enroll: gateway returned an empty api_key")
    return api_key


def _resolve_api_key() -> str | None:
    """Resolve the tenant key: explicit > persisted > self-enroll. Never raises.

    Returns ``None`` when push is disabled or auto-enrollment fails (logged), so
    callers cleanly no-op. An explicit ``settings.push_api_key`` wins and is
    never persisted; otherwise a key persisted by a previous start/worker is
    reused; otherwise we self-enroll once and persist the issued key.
    """
    if not push_enabled():
        return None
    if settings.push_api_key:
        return settings.push_api_key

    global _cached_api_key
    if _cached_api_key is not None:
        return _cached_api_key

    persisted = _read_persisted_key()
    if persisted:
        _cached_api_key = persisted
        return _cached_api_key

    with _key_lock:
        # Re-check under the lock: another thread may have enrolled meanwhile.
        if _cached_api_key is not None:
            return _cached_api_key
        persisted = _read_persisted_key()
        if persisted:
            _cached_api_key = persisted
            return _cached_api_key
        try:
            issued = enroll()
        except PushGatewayError:
            logger.warning("push: auto-enroll with gateway failed", exc_info=True)
            return None
        _cached_api_key = _persist_key(issued)
        logger.info("push: auto-enrolled with gateway; key persisted")
        return _cached_api_key


def _authed_request(method: str, path: str, payload: dict) -> httpx.Response:
    """Resolve the key and issue an authenticated gateway request (raises)."""
    api_key = _resolve_api_key()
    if api_key is None:
        raise PushGatewayError("push not configured")
    return _http(
        method,
        f"{_gateway_url()}{path}",
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
    )


# --- public best-effort API (sync; safe for the Celery poller) -------------


def register_device(
    user_id: str,
    device_token: str,
    *,
    device_id: str | None = None,
    platform: str = "ios",
) -> bool:
    """Register/refresh a device token with the gateway (upsert). Best-effort.

    Returns ``True`` on success, ``False`` when push is disabled or the gateway
    call failed (logged, never raised).
    """
    if not push_enabled():
        return False
    payload: dict[str, object] = {
        "user_id": user_id,
        "platform": platform,
        "token": device_token,
        "topic": PUSH_TOPIC,
    }
    if device_id:
        payload["device_id"] = device_id
    try:
        _authed_request("POST", "/v1/devices", payload)
        return True
    except PushGatewayError:
        logger.warning("push: register device failed", exc_info=True)
        return False


def unregister_device(
    *,
    device_token: str | None = None,
    device_id: str | None = None,
    platform: str = "ios",
) -> bool:
    """Remove a device from the gateway by ``device_id`` or ``(platform, token)``.

    Best-effort: returns ``False`` (no raise) when push is disabled, neither
    identifier was given, or the gateway call failed.
    """
    if not push_enabled():
        return False
    payload: dict[str, object]
    if device_id:
        payload = {"device_id": device_id}
    elif device_token:
        payload = {"platform": platform, "token": device_token}
    else:
        return False
    try:
        _authed_request("DELETE", "/v1/devices", payload)
        return True
    except PushGatewayError:
        logger.warning("push: unregister device failed", exc_info=True)
        return False


def send_to_users(
    user_ids: list[str],
    title: str,
    body: str,
    data: dict | None = None,
) -> bool:
    """Send a high-priority alert to the given users' devices. Best-effort.

    Targets ``user_ids`` (tenant keys cannot target raw tokens). ``data`` rides
    alongside the alert for client deep-linking. Returns ``True`` when the
    gateway accepted the send, ``False`` when push is disabled, there are no
    recipients, or the call failed (logged, never raised) — so a notification
    failure can never break the caller (poller / endpoint).
    """
    if not push_enabled() or not user_ids:
        return False
    payload: dict[str, object] = {
        "to": {"user_ids": user_ids},
        "notification": {"title": title, "body": body},
        "data": data or {},
        "options": {"priority": "high"},
        "apns": {"topic": PUSH_TOPIC},
    }
    try:
        _authed_request("POST", "/v1/notifications", payload)
        return True
    except PushGatewayError:
        logger.warning("push: send notification failed", exc_info=True)
        return False


# --- async wrappers (for FastAPI endpoints) --------------------------------


async def register_device_async(
    user_id: str,
    device_token: str,
    *,
    device_id: str | None = None,
    platform: str = "ios",
) -> bool:
    """Async wrapper over :func:`register_device` (runs off the event loop)."""
    return await asyncio.to_thread(
        register_device,
        user_id,
        device_token,
        device_id=device_id,
        platform=platform,
    )


async def unregister_device_async(
    *,
    device_token: str | None = None,
    device_id: str | None = None,
    platform: str = "ios",
) -> bool:
    """Async wrapper over :func:`unregister_device` (runs off the event loop)."""
    return await asyncio.to_thread(
        unregister_device,
        device_token=device_token,
        device_id=device_id,
        platform=platform,
    )


def _reset_cache() -> None:
    """Test hook: drop the in-process key cache (the file stays authoritative)."""
    global _cached_api_key
    _cached_api_key = None
