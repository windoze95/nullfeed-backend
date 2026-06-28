import asyncio
import hashlib
import hmac
import logging
import math
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session_factory, get_db
from app.models.recommendation import Recommendation
from app.models.session import Session
from app.models.subscription import UserSubscription
from app.models.user import User
from app.models.user_queue import UserQueue
from app.models.user_video_ref import UserVideoRef
from app.schemas.ticket import AccessTicket
from app.schemas.user import (
    UserCreate,
    UserProfile,
    UserSelect,
    UserSession,
    UserUpdate,
)
from app.services.storage import check_and_delete_orphan
from app.utils.tickets import SCOPE_WS, mint_ticket
from app.utils.time import utcnow_naive

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger(__name__)

# scrypt parameters for PIN hashing
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1

# Refresh sessions.last_seen_at at most this often.
_LAST_SEEN_REFRESH = timedelta(hours=1)

# --- PIN brute-force throttle ----------------------------------------------
#
# PIN attempts are rate-limited per (client IP, target user) with exponential
# backoff. Keying on the IP — not the user id alone — is deliberate: the old
# per-user lock could be tripped by ANYONE who knew a user_id, and /profiles
# lists every user_id, so an attacker could lock a household member out at will
# (an account-lockout DoS). Folding the user_id into the key as well keeps a
# shared household device able to switch to another profile while one is backed
# off, and still throttles guessing of any single PIN. Because attacker and
# victim have different IPs, an attacker can no longer lock out the victim.
#
# The store is in-process: counters are lost on restart and NOT shared across
# workers. Kept this way on purpose so tests / local dev need no running Redis.
# TODO: back this with the existing Redis (settings.redis_url) for multi-worker
# coordination and restart persistence once a Redis is guaranteed in all envs.
_PIN_MAX_FAILURES = 5  # failures allowed before the first lockout kicks in
_PIN_LOCKOUT_BASE_SECONDS = 30.0  # first lockout window; doubles each failure
_PIN_LOCKOUT_MAX_SECONDS = 3600.0  # cap on the exponential backoff (1 hour)
_PIN_FAILURE_RESET_SECONDS = 3600.0  # forget an idle key's history after this
_PIN_THROTTLE_SOFT_CAP = 1024  # prune stale keys once the map grows past this


@dataclass
class _PinThrottle:
    """Per-key brute-force state. Times are ``time.monotonic`` seconds."""

    failures: int = 0
    locked_until: float = 0.0  # 0.0 means not currently locked
    last_failure: float = 0.0


_pin_throttle: dict[str, _PinThrottle] = {}


def _hash_pin(pin: str) -> str:
    """Hash a PIN with scrypt and a per-user random salt."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        pin.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


# A valid scrypt hash of a random, unguessable secret. When /select is given a
# user_id that does not exist we verify the supplied PIN against this instead of
# returning early, so a missing user costs the same scrypt work — and yields the
# same response — as a wrong PIN. It can never match a real attempt.
_DUMMY_PIN_HASH = _hash_pin(secrets.token_urlsafe(32))


def _is_legacy_pin_hash(stored: str) -> bool:
    """Legacy hashes are plain SHA-256 hex digests (no '$' separators)."""
    return "$" not in stored


def _verify_pin(pin: str, stored: str) -> bool:
    if _is_legacy_pin_hash(stored):
        legacy = hashlib.sha256(pin.encode()).hexdigest()
        return hmac.compare_digest(legacy, stored)
    try:
        scheme, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            pin.encode(),
            salt=bytes.fromhex(salt_hex),
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
        )
    except ValueError:
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _client_ip(request: Request) -> str:
    """Best-effort client IP used to key PIN rate limiting.

    Defaults to the real TCP peer (``request.client.host``), which the client
    cannot forge. Only when ``settings.trust_proxy_headers`` is set — i.e. the
    operator has put NullFeed behind a single trusted reverse proxy that appends
    ``X-Forwarded-For`` — do we use the rightmost XFF entry, which is the address
    that trusted proxy actually observed. The leftmost entries are never trusted
    because a client can forge them. Do NOT enable ``trust_proxy_headers`` if
    clients can reach the app directly: they could then spoof their rate-limit
    key and evade the throttle.
    """
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if hops:
            return hops[-1]
    if request.client is not None:
        return request.client.host
    return "unknown"


def _pin_rate_key(request: Request, user_id: str) -> str:
    """Throttle key: client IP + target user_id (neither contains '|')."""
    return f"{_client_ip(request)}|{user_id}"


def _prune_pin_throttle(now: float) -> None:
    """Drop long-idle entries so the map can't grow without bound (IP churn)."""
    if len(_pin_throttle) <= _PIN_THROTTLE_SOFT_CAP:
        return
    stale = [
        key
        for key, state in _pin_throttle.items()
        if now >= state.locked_until
        and now - state.last_failure >= _PIN_FAILURE_RESET_SECONDS
    ]
    for key in stale:
        _pin_throttle.pop(key, None)


def _check_pin_rate_limit(key: str) -> None:
    """Raise 429 (with Retry-After) while this key is in a backoff window."""
    state = _pin_throttle.get(key)
    if state is None:
        return
    now = time.monotonic()
    if state.locked_until and now < state.locked_until:
        retry_after = max(1, math.ceil(state.locked_until - now))
        raise HTTPException(
            status_code=429,
            detail="Too many failed PIN attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    # The lockout (if any) has elapsed. Once a key has been idle long enough,
    # forget it so honest users get a clean slate; otherwise keep the running
    # failure count so continued abuse keeps escalating the backoff.
    if now - state.last_failure >= _PIN_FAILURE_RESET_SECONDS:
        _pin_throttle.pop(key, None)


def _record_pin_failure(key: str) -> None:
    """Count a failed PIN attempt and (re)arm an exponential backoff lockout."""
    now = time.monotonic()
    state = _pin_throttle.setdefault(key, _PinThrottle())
    state.failures += 1
    state.last_failure = now
    if state.failures >= _PIN_MAX_FAILURES:
        backoff = min(
            _PIN_LOCKOUT_BASE_SECONDS * 2 ** (state.failures - _PIN_MAX_FAILURES),
            _PIN_LOCKOUT_MAX_SECONDS,
        )
        state.locked_until = now + backoff
    _prune_pin_throttle(now)


def _reset_pin_throttle(key: str) -> None:
    """Clear a single key's history (called after a correct PIN)."""
    _pin_throttle.pop(key, None)


def _reset_pin_throttle_for_user(user_id: str) -> None:
    """Clear every IP's throttle for a user (PIN changed/removed or deleted).

    Without this, an admin resetting a forgotten PIN would leave the locked-out
    device backing off until the window elapses.
    """
    suffix = f"|{user_id}"
    for key in [k for k in _pin_throttle if k.endswith(suffix)]:
        _pin_throttle.pop(key, None)


def _session_is_expired(session: Session, now: datetime) -> bool:
    """True once a session is past its absolute lifetime or has gone idle."""
    absolute_ttl = timedelta(days=settings.session_absolute_ttl_days)
    idle_ttl = timedelta(days=settings.session_idle_ttl_days)
    if session.created_at is not None and now - session.created_at >= absolute_ttl:
        return True
    if session.last_seen_at is not None and now - session.last_seen_at >= idle_ttl:
        return True
    return False


async def _resolve_session(token: str, db: AsyncSession) -> Session | None:
    """Resolve a session by raw token, enforcing absolute + idle expiry.

    Returns None for a missing OR expired session; every caller treats that as
    unauthenticated. Live sessions get last_seen_at refreshed at most hourly to
    bound write volume; expired rows are deleted out-of-band by the reaper.
    """
    result = await db.execute(
        select(Session).where(Session.token_hash == _hash_token(token))
    )
    session = result.scalar_one_or_none()
    if session is None:
        return None
    now = utcnow_naive()
    if _session_is_expired(session, now):
        return None
    if session.last_seen_at is None or now - session.last_seen_at >= _LAST_SEEN_REFRESH:
        session.last_seen_at = now
        await db.commit()
    return session


async def get_current_user(
    x_user_token: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Resolve the current user from the X-User-Token header."""
    if not x_user_token:
        raise HTTPException(status_code=401, detail="Missing X-User-Token header")
    session = await _resolve_session(x_user_token, db)
    if session is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session token")
    result = await db.execute(select(User).where(User.id == session.user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


async def validate_token(token: str) -> str | None:
    """Look up a session token and return the user_id, or None."""
    async with async_session_factory() as db:
        session = await _resolve_session(token, db)
        return session.user_id if session else None


def _write_bytes(path: str, data: bytes) -> None:
    Path(path).write_bytes(data)


async def cache_avatar(url: str, user_id: str) -> str | None:
    """Download an avatar image and cache it under the thumbnails path.

    Returns the public URL path of the cached file, or None on any failure.
    """
    avatar_dir = os.path.join(settings.thumbnails_path, "avatars")
    dest = os.path.join(avatar_dir, f"{user_id}.jpg")
    try:
        os.makedirs(avatar_dir, exist_ok=True)
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
        await asyncio.to_thread(_write_bytes, dest, response.content)
    except Exception:
        logger.warning("Failed to cache avatar from %s", url, exc_info=True)
        return None
    return f"/data/thumbnails/avatars/{user_id}.jpg"


@router.post("/profiles", response_model=list[UserProfile])
async def list_profiles(db: AsyncSession = Depends(get_db)) -> list[UserProfile]:
    result = await db.execute(select(User).order_by(User.created_at))
    users = result.scalars().all()
    return [UserProfile.model_validate(u) for u in users]


@router.post("/select", response_model=UserSession)
async def select_profile(
    body: UserSelect,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> UserSession:
    rate_key = _pin_rate_key(request, body.user_id)
    _check_pin_rate_limit(rate_key)

    result = await db.execute(select(User).where(User.id == body.user_id))
    user = result.scalar_one_or_none()

    # A missing user is treated exactly like a PIN-protected user whose PIN can
    # never match: same status, message, and scrypt cost, so /select can't be
    # used as a user-existence/timing oracle. (PIN-less users that DO exist stay
    # distinguishable, but that is inherent — they carry no secret — and is
    # already public via /profiles.)
    stored_hash = user.pin_hash if user is not None else _DUMMY_PIN_HASH

    if stored_hash:
        if not body.pin:
            raise HTTPException(status_code=403, detail="PIN required")
        if not _verify_pin(body.pin, stored_hash):
            _record_pin_failure(rate_key)
            raise HTTPException(status_code=403, detail="Incorrect PIN")
        _reset_pin_throttle(rate_key)
        if user is None:
            # _DUMMY_PIN_HASH must never verify; refuse defensively if it did.
            raise HTTPException(status_code=403, detail="Incorrect PIN")
        # Upgrade legacy SHA-256 hashes to scrypt on successful verification.
        # (stored_hash is user.pin_hash here, narrowed to str by `if stored_hash`.)
        if _is_legacy_pin_hash(stored_hash):
            user.pin_hash = _hash_pin(body.pin)

    # Reached only for a PIN-less existing user or a verified PIN, so the user
    # row is always present here.
    assert user is not None
    token = secrets.token_urlsafe(32)
    db.add(Session(token_hash=_hash_token(token), user_id=user.id))
    await db.commit()
    return UserSession(user=UserProfile.model_validate(user), token=token)


@router.post("/create", response_model=UserProfile)
async def create_profile(
    body: UserCreate,
    db: AsyncSession = Depends(get_db),
) -> UserProfile:
    user_id = str(uuid.uuid4())
    display_name = body.display_name
    avatar_url = body.avatar_url

    youtube_handle = (body.youtube_handle or "").strip()
    if youtube_handle:
        # Lazy import: the resolver pulls in yt-dlp subprocess machinery.
        from app.services.youtube_import import YoutubeResolveError, resolve_handle

        try:
            identity = await resolve_handle(youtube_handle)
        except YoutubeResolveError:
            raise HTTPException(
                status_code=502, detail="Could not resolve YouTube handle"
            )
        if display_name is None:
            resolved_name = (identity.get("name") or "").strip()
            display_name = (resolved_name or youtube_handle.lstrip("@"))[:50]
        avatar_url = None
        if identity.get("avatar_url"):
            avatar_url = await cache_avatar(identity["avatar_url"], user_id)

    if not display_name:
        raise HTTPException(status_code=422, detail="display_name is required")

    # First user ever created becomes admin; everyone else does not. Counted
    # here — after the (slow) YouTube resolve — so a concurrent create during
    # that window cannot also observe an empty table and mint a second admin.
    result = await db.execute(select(func.count()).select_from(User))
    is_first_user = result.scalar_one() == 0

    user = User(
        id=user_id,
        display_name=display_name,
        avatar_url=avatar_url,
        pin_hash=_hash_pin(body.pin) if body.pin else None,
        is_admin=is_first_user,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return UserProfile.model_validate(user)


@router.get("/me", response_model=UserProfile)
async def get_me(user: User = Depends(get_current_user)) -> UserProfile:
    return UserProfile.model_validate(user)


@router.post("/ws-ticket", response_model=AccessTicket)
async def create_ws_ticket(user: User = Depends(get_current_user)) -> AccessTicket:
    """Mint a short-lived, user-scoped ticket for the WebSocket handshake (#30).

    Session-authenticated; the returned ticket is passed to ``/ws/{user_id}`` as
    ``?ticket=`` so the long-lived session token never rides the handshake URL.
    """
    ticket, expires_in = mint_ticket(SCOPE_WS, user.id)
    return AccessTicket(ticket=ticket, expires_in=expires_in)


@router.post("/logout")
async def logout(
    x_user_token: str | None = Header(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if x_user_token:
        await db.execute(
            delete(Session).where(Session.token_hash == _hash_token(x_user_token))
        )
        await db.commit()
    return {"detail": "Logged out"}


@router.delete("/sessions")
async def logout_all_devices(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Revoke every session for the current user (log out all devices)."""
    await db.execute(delete(Session).where(Session.user_id == user.id))
    await db.commit()
    return {"detail": "All sessions revoked"}


@router.patch("/profiles/{user_id}", response_model=UserProfile)
async def update_profile(
    user_id: str,
    body: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserProfile:
    if current_user.id != user_id and not current_user.is_admin:
        raise HTTPException(
            status_code=403, detail="Not authorized to modify this profile"
        )

    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if body.display_name is not None:
        target.display_name = body.display_name
    if body.avatar_url is not None:
        target.avatar_url = body.avatar_url
    if body.remove_pin:
        target.pin_hash = None
        _reset_pin_throttle_for_user(target.id)
    elif body.pin is not None:
        target.pin_hash = _hash_pin(body.pin)
        _reset_pin_throttle_for_user(target.id)

    await db.commit()
    await db.refresh(target)
    return UserProfile.model_validate(target)


@router.delete("/profiles/{user_id}")
async def delete_profile(
    user_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if current_user.id != user_id and not current_user.is_admin:
        raise HTTPException(
            status_code=403, detail="Not authorized to delete this profile"
        )

    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if target.is_admin:
        others = await db.execute(
            select(func.count()).select_from(User).where(User.id != user_id)
        )
        other_admins = await db.execute(
            select(func.count())
            .select_from(User)
            .where(User.id != user_id, User.is_admin.is_(True))
        )
        if others.scalar_one() > 0 and other_admins.scalar_one() == 0:
            raise HTTPException(
                status_code=409, detail="Cannot delete the only admin profile"
            )

    # Collect referenced videos before removing the refs, for orphan cleanup.
    refs_result = await db.execute(
        select(UserVideoRef.video_id).where(UserVideoRef.user_id == user_id)
    )
    video_ids = list(refs_result.scalars().all())

    await db.execute(delete(UserVideoRef).where(UserVideoRef.user_id == user_id))
    await db.execute(delete(UserQueue).where(UserQueue.user_id == user_id))
    await db.execute(
        delete(UserSubscription).where(UserSubscription.user_id == user_id)
    )
    await db.execute(delete(Recommendation).where(Recommendation.user_id == user_id))
    await db.execute(delete(Session).where(Session.user_id == user_id))
    await db.execute(delete(User).where(User.id == user_id))
    await db.commit()

    for video_id in video_ids:
        try:
            await check_and_delete_orphan(video_id, db)
        except Exception:
            logger.exception("Orphan cleanup failed for video %s", video_id)

    _reset_pin_throttle_for_user(user_id)
    return {"detail": "Profile deleted"}
