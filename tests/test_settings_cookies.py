"""Admin YouTube-cookies settings endpoints (in-app cookie management)."""

import pytest

import app.api.settings as settings_api
from app.config import settings
from app.utils import ytdlp

pytestmark = pytest.mark.asyncio

COOKIE = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n"


@pytest.fixture(autouse=True)
def _cookies_in_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "youtube_cookies_file", "")
    monkeypatch.setattr(settings, "config_path", str(tmp_path))
    # Don't shell out to yt-dlp in tests; default to "cookies work".
    monkeypatch.setattr(settings_api, "verify_cookies", lambda: None)


async def test_requires_admin(client, make_user):
    await make_user("Admin")  # first profile created is the admin
    _, viewer_headers = await make_user("Viewer")  # subsequent profiles are not
    resp = await client.get("/api/settings/youtube-cookies", headers=viewer_headers)
    assert resp.status_code == 403


async def test_put_get_delete_cookies(client, make_user):
    _, headers = await make_user("Admin")

    resp = await client.get("/api/settings/youtube-cookies", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["configured"] is False

    resp = await client.put(
        "/api/settings/youtube-cookies", json={"cookies": COOKIE}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["configured"] is True
    assert ytdlp.cookies_path() is not None

    resp = await client.delete("/api/settings/youtube-cookies", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["configured"] is False


async def test_put_reports_when_cookies_dont_work(client, make_user, monkeypatch):
    """Saving stores the file but the status reflects a failed verification, so
    the UI won't claim "connected" when the cookies don't actually work."""
    _, headers = await make_user("Admin")
    monkeypatch.setattr(
        settings_api,
        "verify_cookies",
        lambda: "ERROR: Sign in to confirm you're not a bot",
    )
    resp = await client.put(
        "/api/settings/youtube-cookies", json={"cookies": COOKIE}, headers=headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["stale"] is True
    assert "bot" in (body["last_error"] or "")


async def test_rejects_non_cookie_paste(client, make_user):
    _, headers = await make_user("Admin")
    resp = await client.put(
        "/api/settings/youtube-cookies",
        json={"cookies": "just some random words, not a cookies file"},
        headers=headers,
    )
    assert resp.status_code == 400
