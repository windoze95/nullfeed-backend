# ruff: noqa: E402
"""Shared fixtures for the backend test suite.

Path and database environment variables are set BEFORE any app import so
that ``app.config.Settings`` picks up temp paths — the real defaults point
at /data, which only exists inside the Docker container.
"""

import os
import tempfile

_TMP_ROOT = tempfile.mkdtemp(prefix="nullfeed-tests-")

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{os.path.join(_TMP_ROOT, 'test.db')}"
os.environ["MEDIA_PATH"] = os.path.join(_TMP_ROOT, "media")
os.environ["DB_PATH"] = os.path.join(_TMP_ROOT, "db")
os.environ["CONFIG_PATH"] = os.path.join(_TMP_ROOT, "config")
os.environ["THUMBNAILS_PATH"] = os.path.join(_TMP_ROOT, "thumbnails")
# Disable push by default so the suite never reaches the real gateway; the push
# tests opt in by monkeypatching settings.push_gateway_url.
os.environ["NULLFEED_PUSH_GATEWAY_URL"] = ""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import app.api.auth as auth_api
import app.api.websocket as websocket_api
import app.services.chatgpt_auth as chatgpt_auth
import app.services.discovery as discovery_service
import app.services.instant_stream as instant_stream
import app.services.push_gateway as push_gateway
import app.services.youtube_import as youtube_import
import app.utils.websub as websub_util
from app.database import engine
from app.main import app
from app.models import Base


@pytest.fixture(autouse=True)
def _reset_in_memory_state():
    """Clear per-process caches/counters that would leak between tests."""
    yield
    auth_api._pin_throttle.clear()
    # Locks are bound to the event loop that created them; each test gets a
    # fresh loop, so a leaked lock would raise 'attached to a different loop'.
    discovery_service._generation_locks.clear()
    chatgpt_auth._reset_state()
    chatgpt_auth.clear_auth()
    youtube_import._resolve_cache.clear()
    youtube_import._suggestions_cache.clear()
    instant_stream._resolve_cache.clear()
    websocket_api._connections.clear()
    push_gateway._reset_cache()
    websub_util._reset_cache()


@pytest_asyncio.fixture
async def _database():
    """Fresh tables for the test; dispose pooled connections afterwards.

    Disposing matters: each test runs in its own event loop, and aiosqlite
    connections are bound to the loop that created them.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def client(_database):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def make_user(client):
    """Factory: create a profile, select it, return (profile, auth headers)."""

    async def _make(
        display_name: str = "User", pin: str | None = None
    ) -> tuple[dict, dict]:
        payload: dict = {"display_name": display_name}
        if pin is not None:
            payload["pin"] = pin
        resp = await client.post("/api/auth/create", json=payload)
        assert resp.status_code == 200, resp.text
        profile = resp.json()

        select_payload: dict = {"user_id": profile["id"]}
        if pin is not None:
            select_payload["pin"] = pin
        resp = await client.post("/api/auth/select", json=select_payload)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        return data["user"], {"X-User-Token": data["token"]}

    return _make
