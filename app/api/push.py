"""Device push-token endpoints (#33).

A client registers its APNs device token here after the user grants push
permission; the backend forwards it to the shared push gateway scoped to the
current user (so a later new-episode notification can target ``user_ids``). On
logout, or when the OS reports a token is stale, the client unregisters it.

Both endpoints are session-authenticated and best-effort: the gateway call never
raises into the response, and when push is disabled (no gateway configured) they
no-op with ``{"enabled": false}`` rather than erroring, so a client can probe
support and degrade gracefully.
"""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.auth import get_current_user
from app.models.user import User
from app.services import push_gateway

router = APIRouter(prefix="/api/push", tags=["push"])
logger = logging.getLogger(__name__)


class DeviceRegistration(BaseModel):
    device_token: str
    device_id: str | None = None
    platform: str = "ios"


class DeviceUnregistration(BaseModel):
    device_token: str | None = None
    device_id: str | None = None
    platform: str = "ios"


@router.post("/register")
async def register_device(
    body: DeviceRegistration,
    user: User = Depends(get_current_user),
) -> dict:
    """Register this device's APNs token for the current user.

    Forwards to the gateway ``POST /v1/devices`` with the caller's user id and
    the fixed NullFeed topic. No-op (``{"enabled": false}``) when push is
    disabled.
    """
    if not push_gateway.push_enabled():
        return {"enabled": False}
    registered = await push_gateway.register_device_async(
        user.id,
        body.device_token,
        device_id=body.device_id,
        platform=body.platform,
    )
    return {"enabled": True, "registered": registered}


@router.delete("/register")
async def unregister_device(
    body: DeviceUnregistration,
    user: User = Depends(get_current_user),
) -> dict:
    """Remove this device's token from the gateway (logout / stale token).

    Identify the device by ``device_id`` or ``(platform, device_token)``. No-op
    (``{"enabled": false}``) when push is disabled.
    """
    if not push_gateway.push_enabled():
        return {"enabled": False}
    unregistered = await push_gateway.unregister_device_async(
        device_token=body.device_token,
        device_id=body.device_id,
        platform=body.platform,
    )
    return {"enabled": True, "unregistered": unregistered}
