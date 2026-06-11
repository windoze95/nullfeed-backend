import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session_factory, get_db
from app.models.recommendation import Recommendation
from app.models.session import Session
from app.models.subscription import UserSubscription
from app.models.user import User
from app.models.user_video_ref import UserVideoRef
from app.schemas.user import (
    UserCreate,
    UserProfile,
    UserSelect,
    UserSession,
    UserUpdate,
)
from app.services.storage import check_and_delete_orphan
from app.utils.time import utcnow_naive

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger(__name__)

# scrypt parameters for PIN hashing
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1

# Refresh sessions.last_seen_at at most this often.
_LAST_SEEN_REFRESH = timedelta(hours=1)

# In-memory PIN rate limiting: after N consecutive failures, lock for a bit.
_PIN_MAX_FAILURES = 5
_PIN_LOCKOUT_SECONDS = 30.0
_pin_failures: dict[str, int] = {}
_pin_lockouts: dict[str, float] = {}


def _hash_pin(pin: str) -> str:
    """Hash a PIN with scrypt and a per-user random salt."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        pin.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


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


def _check_pin_rate_limit(user_id: str) -> None:
    """Raise 429 while the user is locked out from PIN attempts."""
    locked_until = _pin_lockouts.get(user_id)
    if locked_until is None:
        return
    if time.monotonic() < locked_until:
        raise HTTPException(
            status_code=429,
            detail="Too many failed PIN attempts. Try again in 30 seconds.",
        )
    _pin_lockouts.pop(user_id, None)
    _pin_failures.pop(user_id, None)


def _record_pin_failure(user_id: str) -> None:
    count = _pin_failures.get(user_id, 0) + 1
    _pin_failures[user_id] = count
    if count >= _PIN_MAX_FAILURES:
        _pin_lockouts[user_id] = time.monotonic() + _PIN_LOCKOUT_SECONDS


def _clear_pin_failures(user_id: str) -> None:
    _pin_failures.pop(user_id, None)
    _pin_lockouts.pop(user_id, None)


async def _resolve_session(token: str, db: AsyncSession) -> Session | None:
    """Look up a persistent session by raw token; refresh last_seen_at hourly."""
    result = await db.execute(
        select(Session).where(Session.token_hash == _hash_token(token))
    )
    session = result.scalar_one_or_none()
    if session is None:
        return None
    now = utcnow_naive()
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
    db: AsyncSession = Depends(get_db),
) -> UserSession:
    result = await db.execute(select(User).where(User.id == body.user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.pin_hash:
        _check_pin_rate_limit(user.id)
        if not body.pin:
            raise HTTPException(status_code=403, detail="PIN required")
        if not _verify_pin(body.pin, user.pin_hash):
            _record_pin_failure(user.id)
            raise HTTPException(status_code=403, detail="Incorrect PIN")
        _clear_pin_failures(user.id)
        # Upgrade legacy SHA-256 hashes to scrypt on successful verification.
        if _is_legacy_pin_hash(user.pin_hash):
            user.pin_hash = _hash_pin(body.pin)

    token = secrets.token_urlsafe(32)
    db.add(Session(token_hash=_hash_token(token), user_id=user.id))
    await db.commit()
    return UserSession(user=UserProfile.model_validate(user), token=token)


@router.post("/create", response_model=UserProfile)
async def create_profile(
    body: UserCreate,
    db: AsyncSession = Depends(get_db),
) -> UserProfile:
    # First user ever created becomes admin; everyone else does not.
    result = await db.execute(select(func.count()).select_from(User))
    is_first_user = result.scalar_one() == 0

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
        _clear_pin_failures(target.id)
    elif body.pin is not None:
        target.pin_hash = _hash_pin(body.pin)
        _clear_pin_failures(target.id)

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

    _clear_pin_failures(user_id)
    return {"detail": "Profile deleted"}
