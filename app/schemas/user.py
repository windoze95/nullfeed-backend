import re
from datetime import datetime

from pydantic import BaseModel, field_validator, model_validator

_PIN_PATTERN = re.compile(r"^\d{4,8}$")


def _clean_display_name(value: str) -> str:
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= 50:
        raise ValueError("display_name must be 1-50 characters")
    return cleaned


def _check_pin(value: str) -> str:
    if not _PIN_PATTERN.fullmatch(value):
        raise ValueError("PIN must be 4-8 digits")
    return value


class UserProfile(BaseModel):
    id: str
    display_name: str
    avatar_url: str | None = None
    is_admin: bool = False
    has_pin: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


class UserCreate(BaseModel):
    display_name: str | None = None
    avatar_url: str | None = None
    pin: str | None = None
    youtube_handle: str | None = None

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_display_name(value)

    @field_validator("pin")
    @classmethod
    def validate_pin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _check_pin(value)

    @model_validator(mode="after")
    def require_name_or_handle(self) -> "UserCreate":
        if self.display_name is None and not (self.youtube_handle or "").strip():
            raise ValueError("display_name is required unless youtube_handle is given")
        return self


class UserUpdate(BaseModel):
    display_name: str | None = None
    avatar_url: str | None = None
    pin: str | None = None
    remove_pin: bool = False

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_display_name(value)

    @field_validator("pin")
    @classmethod
    def validate_pin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _check_pin(value)


class UserSelect(BaseModel):
    user_id: str
    pin: str | None = None


class UserSession(BaseModel):
    user: UserProfile
    token: str
