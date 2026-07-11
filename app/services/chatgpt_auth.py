"""ChatGPT-subscription sign-in for the discovery rank provider (Codex OAuth).

Implements the device-code "sign in with ChatGPT" flow that Codex CLI uses
(and that agents like opencode and Hermes ride) so a self-hosted NullFeed can
run Discover ranking against a ChatGPT Plus/Pro plan instead of API credits.

This is an UNOFFICIAL surface: OpenAI operates it for Codex clients and has
tolerated third-party use, but it is undocumented and may change or be
gated at any time. Everything here degrades gracefully — a broken login only
disables the ``chatgpt`` rank provider; Discover falls back like any other
missing provider.

Flow (mirrors codex-rs/login):

1. ``POST /api/accounts/deviceauth/usercode`` -> ``{device_auth_id,
   user_code, interval}``. The admin opens ``https://auth.openai.com/codex/device``
   and enters the code. Device authorization is OFF by default — the account
   owner must enable it in ChatGPT security settings first.
2. Poll ``POST /api/accounts/deviceauth/token`` (403/404 = not yet approved)
   until it returns ``{authorization_code, code_verifier}``.
3. Exchange the code at ``/oauth/token`` (PKCE, form-encoded). Tokens are
   persisted 0600 under ``settings.config_path``.

Refresh tokens are single-use and rotated on every refresh, so refreshes are
serialized behind a lock and the store is written atomically; a re-auth-class
error (``invalid_grant`` / ``refresh_token_*``) marks the store
``needs_reauth`` instead of deleting it, so the admin UI can say why.
"""

import asyncio
import base64
import binascii
import json
import logging
import os
import time
from pathlib import Path

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_BASE = "https://auth.openai.com"
TOKEN_URL = f"{AUTH_BASE}/oauth/token"
VERIFICATION_URL = f"{AUTH_BASE}/codex/device"

_AUTH_FILENAME = "chatgpt_codex_auth.json"
_PENDING_FILENAME = "chatgpt_codex_pending.json"
# Refresh proactively when the access token expires within this window.
_REFRESH_WINDOW_SECONDS = 300
# The device code is valid for 15 minutes (codex-rs constant).
_DEVICE_FLOW_TTL_SECONDS = 900
_TIMEOUT = 30.0

# Token-endpoint error codes that mean the refresh token is dead and the
# admin must sign in again (single-use rotation makes these expected when
# another Codex client consumed the same credential).
_REAUTH_ERROR_CODES = (
    "invalid_grant",
    "invalid_token",
    "refresh_token_expired",
    "refresh_token_reused",
    "refresh_token_invalidated",
)


class DeviceLoginError(RuntimeError):
    """The device-auth flow could not be started or completed."""


# Serialized refresh: the lock is loop-bound, so tests reset it per loop.
_refresh_lock: asyncio.Lock | None = None


def _get_refresh_lock() -> asyncio.Lock:
    global _refresh_lock
    if _refresh_lock is None:
        _refresh_lock = asyncio.Lock()
    return _refresh_lock


def _reset_state() -> None:
    """Test hook: drop the loop-bound lock between event loops."""
    global _refresh_lock
    _refresh_lock = None


def _auth_path() -> Path:
    return Path(settings.config_path) / _AUTH_FILENAME


def _pending_path() -> Path:
    return Path(settings.config_path) / _PENDING_FILENAME


def _write_private(path: Path, data: dict) -> None:
    """Atomic 0600 write (tmp + rename), mirroring the push-key pattern."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    tmp.write_text(json.dumps(data))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _load(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _jwt_payload(token: str) -> dict:
    """Decode a JWT payload WITHOUT signature verification.

    We only read our own token's expiry and account claim locally; the
    backend is what actually authenticates the token.
    """
    try:
        segment = token.split(".")[1]
        padded = segment + "=" * (-len(segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError, binascii.Error):
        return {}
    return payload if isinstance(payload, dict) else {}


def _jwt_exp(token: str) -> float | None:
    exp = _jwt_payload(token).get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


def _account_id_from(record: dict) -> str | None:
    for key in ("access_token", "id_token"):
        claims = _jwt_payload(record.get(key) or "")
        auth_claim = claims.get("https://api.openai.com/auth")
        if isinstance(auth_claim, dict):
            account_id = auth_claim.get("chatgpt_account_id")
            if isinstance(account_id, str) and account_id:
                return account_id
    return None


def has_auth() -> bool:
    """True when a signed-in, non-broken credential store exists."""
    record = _load(_auth_path())
    return bool(
        record and record.get("refresh_token") and not record.get("needs_reauth")
    )


def auth_status() -> dict:
    record = _load(_auth_path())
    pending = _load(_pending_path())
    status = {
        "connected": bool(record and record.get("refresh_token")),
        "needs_reauth": bool(record and record.get("needs_reauth")),
        "account_id": (record or {}).get("account_id"),
        "pending": bool(pending),
    }
    if pending:
        status["user_code"] = pending.get("user_code")
        status["verification_url"] = VERIFICATION_URL
    return status


def clear_auth() -> None:
    _auth_path().unlink(missing_ok=True)
    _pending_path().unlink(missing_ok=True)


async def start_device_login() -> dict:
    """Begin the device flow; returns what the admin must do next."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{AUTH_BASE}/api/accounts/deviceauth/usercode",
            json={"client_id": CLIENT_ID},
        )
    if resp.status_code >= 400:
        raise DeviceLoginError(
            "Could not start the ChatGPT device sign-in "
            f"(HTTP {resp.status_code}). Device authorization is disabled by "
            "default — enable it in ChatGPT Settings -> Security (or your "
            "workspace settings), then try again."
        )
    data = resp.json()
    user_code = data.get("user_code") or data.get("usercode")
    device_auth_id = data.get("device_auth_id")
    if not (isinstance(user_code, str) and isinstance(device_auth_id, str)):
        raise DeviceLoginError("Unexpected response starting device sign-in")
    _write_private(
        _pending_path(),
        {
            "device_auth_id": device_auth_id,
            "user_code": user_code,
            "interval": int(data.get("interval") or 5),
            "started_at": time.time(),
        },
    )
    return {
        "verification_url": VERIFICATION_URL,
        "user_code": user_code,
        "expires_in_minutes": _DEVICE_FLOW_TTL_SECONDS // 60,
    }


async def poll_device_login() -> dict:
    """One poll attempt; the caller (admin UI) repeats until terminal."""
    pending = _load(_pending_path())
    if not pending:
        return {"status": "idle"}
    if time.time() - float(pending.get("started_at") or 0) > _DEVICE_FLOW_TTL_SECONDS:
        _pending_path().unlink(missing_ok=True)
        return {"status": "expired", "detail": "The device code expired; start again."}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{AUTH_BASE}/api/accounts/deviceauth/token",
            json={
                "device_auth_id": pending["device_auth_id"],
                "user_code": pending["user_code"],
            },
        )
        # 403/404 mean "not approved yet" in this (non-RFC-8628) flow.
        if resp.status_code in (403, 404):
            return {
                "status": "pending",
                "user_code": pending["user_code"],
                "verification_url": VERIFICATION_URL,
            }
        if resp.status_code >= 400:
            _pending_path().unlink(missing_ok=True)
            return {
                "status": "error",
                "detail": f"Device sign-in failed (HTTP {resp.status_code}).",
            }
        approval = resp.json()
        token_resp = await _exchange_code(
            client,
            approval.get("authorization_code") or "",
            approval.get("code_verifier") or "",
        )

    _persist_tokens(token_resp)
    _pending_path().unlink(missing_ok=True)
    record = _load(_auth_path()) or {}
    logger.info("ChatGPT sign-in completed (account %s)", record.get("account_id"))
    return {"status": "connected", "account_id": record.get("account_id")}


async def _exchange_code(client: httpx.AsyncClient, code: str, verifier: str) -> dict:
    resp = await client.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"{AUTH_BASE}/deviceauth/callback",
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        },
    )
    if resp.status_code >= 400:
        raise DeviceLoginError(
            f"Token exchange failed (HTTP {resp.status_code}): {resp.text[:200]}"
        )
    data = resp.json()
    if not isinstance(data, dict) or not data.get("access_token"):
        raise DeviceLoginError("Token exchange returned no access token")
    return data


def _persist_tokens(token_resp: dict, existing: dict | None = None) -> None:
    existing = existing or _load(_auth_path()) or {}
    record = {
        "access_token": token_resp.get("access_token") or existing.get("access_token"),
        "refresh_token": token_resp.get("refresh_token")
        or existing.get("refresh_token"),
        "id_token": token_resp.get("id_token") or existing.get("id_token"),
        "last_refresh": time.time(),
        "needs_reauth": False,
    }
    record["account_id"] = _account_id_from(record) or existing.get("account_id")
    _write_private(_auth_path(), record)


def _credentials_from(record: dict) -> tuple[str, str] | None:
    access = record.get("access_token")
    account = record.get("account_id") or _account_id_from(record)
    if isinstance(access, str) and access and isinstance(account, str) and account:
        return access, account
    return None


def _is_fresh(record: dict) -> bool:
    exp = _jwt_exp(record.get("access_token") or "")
    return exp is not None and exp - time.time() > _REFRESH_WINDOW_SECONDS


async def get_access_credentials(
    force_refresh: bool = False,
) -> tuple[str, str] | None:
    """Return (access_token, chatgpt_account_id), refreshing when needed.

    None means signed out or re-auth required — callers treat the provider
    as unavailable.
    """
    record = _load(_auth_path())
    if not record or not record.get("refresh_token") or record.get("needs_reauth"):
        return None
    if not force_refresh and _is_fresh(record):
        return _credentials_from(record)

    async with _get_refresh_lock():
        # Another coroutine may have refreshed while we waited.
        record = _load(_auth_path())
        if not record or not record.get("refresh_token") or record.get("needs_reauth"):
            return None
        if not force_refresh and _is_fresh(record):
            return _credentials_from(record)

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            # JSON body, not form-encoded — matches codex-rs exactly.
            resp = await client.post(
                TOKEN_URL,
                json={
                    "client_id": CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": record["refresh_token"],
                },
            )
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = (resp.json() or {}).get("error") or ""
            except (json.JSONDecodeError, ValueError):
                detail = resp.text[:100]
            if resp.status_code in (400, 401, 403) and (
                not detail or any(code in str(detail) for code in _REAUTH_ERROR_CODES)
            ):
                logger.warning(
                    "ChatGPT token refresh requires re-auth (%s %s)",
                    resp.status_code,
                    detail,
                )
                record["needs_reauth"] = True
                _write_private(_auth_path(), record)
            else:
                logger.warning(
                    "ChatGPT token refresh failed transiently (%s %s)",
                    resp.status_code,
                    detail,
                )
            return None

        _persist_tokens(resp.json(), existing=record)
        refreshed = _load(_auth_path()) or {}
        return _credentials_from(refreshed)
