from datetime import datetime

from pydantic import BaseModel


class UserProfile(BaseModel):
    id: str
    display_name: str
    avatar_url: str | None = None
    is_admin: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


class UserCreate(BaseModel):
    display_name: str
    avatar_url: str | None = None
    pin: str | None = None
    is_admin: bool = False


class UserSelect(BaseModel):
    user_id: str
    pin: str | None = None


class UserSession(BaseModel):
    user: UserProfile
    token: str
