"""Normalized error envelope tests (#3).

Every error response must be a flat {"detail": <str>, "code": <str>} object.
The key backward-compat guarantee is that `detail` is always a STRING (never
FastAPI's default 422 list), so existing clients reading `detail` keep working.
"""

import pytest

pytestmark = pytest.mark.asyncio


async def test_validation_error_detail_is_flat_string(client):
    # PIN too short -> RequestValidationError (would default to a list detail).
    resp = await client.post(
        "/api/auth/create", json={"display_name": "Ok", "pin": "12"}
    )
    assert resp.status_code == 422
    body = resp.json()
    assert set(body) == {"detail", "code"}
    assert isinstance(body["detail"], str)
    assert body["code"] == "validation_error"
    # Flattened to a human-readable message, not a raw error list.
    assert "4-8 digits" in body["detail"]


async def test_validation_error_on_bad_query_param(client, make_user):
    # limit above the allowed range -> query validation error, still flat.
    _, headers = await make_user()
    resp = await client.get("/api/feed/home?limit=999", headers=headers)
    assert resp.status_code == 422
    body = resp.json()
    assert isinstance(body["detail"], str)
    assert body["code"] == "validation_error"


async def test_http_exception_is_enveloped(client):
    # Missing token -> HTTPException(401).
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401
    body = resp.json()
    assert body["detail"] == "Missing X-User-Token header"
    assert body["code"] == "unauthorized"


async def test_http_exception_preserves_custom_detail(client, make_user):
    _, headers = await make_user()
    resp = await client.put(
        "/api/videos/does-not-exist/progress",
        json={"position_seconds": 1},
        headers=headers,
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["detail"] == "Video not found"
    assert body["code"] == "not_found"


async def test_unknown_route_404_is_enveloped(client):
    resp = await client.get("/api/this-route-does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert isinstance(body["detail"], str)
    assert body["code"] == "not_found"
