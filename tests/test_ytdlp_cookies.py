"""YouTube cookies for yt-dlp (age-restricted / members-only auth)."""

import app.services.instant_stream as instant_stream
import app.utils.ytdlp as ytdlp
from app.config import settings


def test_cookie_args_empty_when_no_file(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "youtube_cookies_file", "")
    monkeypatch.setattr(settings, "config_path", str(tmp_path))  # no cookies.txt here
    assert ytdlp.cookie_args() == []


def test_cookie_args_uses_explicit_path(monkeypatch, tmp_path):
    cookies = tmp_path / "my-cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(settings, "youtube_cookies_file", str(cookies))
    assert ytdlp.cookie_args() == ["--cookies", str(cookies)]


def test_cookie_args_falls_back_to_config_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "youtube_cookies_file", "")
    monkeypatch.setattr(settings, "config_path", str(tmp_path))
    (tmp_path / "cookies.txt").write_text("# cookies\n")
    assert ytdlp.cookie_args() == ["--cookies", str(tmp_path / "cookies.txt")]


def test_cookie_args_missing_explicit_path_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "youtube_cookies_file", str(tmp_path / "nope.txt"))
    monkeypatch.setattr(settings, "config_path", str(tmp_path))
    assert ytdlp.cookie_args() == []


def test_resolve_command_includes_cookies(monkeypatch):
    """The instant-stream resolve splices the cookie args into the yt-dlp call."""
    captured: dict = {}

    class _Result:
        returncode = 0
        stdout = "https://upstream.test/v.mp4\n"
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(instant_stream, "cookie_args", lambda: ["--cookies", "/c.txt"])
    monkeypatch.setattr(instant_stream.subprocess, "run", fake_run)
    instant_stream._resolve_cache.clear()

    url = instant_stream.resolve_progressive_url("vid-123")

    assert url == "https://upstream.test/v.mp4"
    assert captured["cmd"][:3] == ["yt-dlp", "--cookies", "/c.txt"]
