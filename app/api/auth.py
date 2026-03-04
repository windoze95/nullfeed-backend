import hashlib
import secrets
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.schemas.user import UserCreate, UserProfile, UserSelect, UserSession

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Simple in-memory session store. In production, use Redis or signed JWTs.
_sessions: dict[str, str] = {}


def _hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()


async def get_current_user(
    x_user_token: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Resolve the current user from the X-User-Token header."""
    if not x_user_token:
        raise HTTPException(status_code=401, detail="Missing X-User-Token header")
    user_id = _sessions.get(x_user_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session token")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def validate_token(token: str) -> str | None:
    """Look up a session token and return the user_id, or None."""
    return _sessions.get(token)


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
        if not body.pin:
            raise HTTPException(status_code=403, detail="PIN required")
        if _hash_pin(body.pin) != user.pin_hash:
            raise HTTPException(status_code=403, detail="Incorrect PIN")

    token = secrets.token_urlsafe(32)
    _sessions[token] = user.id
    return UserSession(user=UserProfile.model_validate(user), token=token)


@router.post("/create", response_model=UserProfile)
async def create_profile(
    body: UserCreate,
    db: AsyncSession = Depends(get_db),
) -> UserProfile:
    # Check if any users exist; first user becomes admin automatically.
    result = await db.execute(select(User))
    existing = result.scalars().all()
    is_first_user = len(existing) == 0

    user = User(
        id=str(uuid.uuid4()),
        display_name=body.display_name,
        avatar_url=body.avatar_url,
        pin_hash=_hash_pin(body.pin) if body.pin else None,
        is_admin=is_first_user or body.is_admin,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return UserProfile.model_validate(user)
