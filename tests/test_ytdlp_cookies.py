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


def test_save_status_and_clear(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "youtube_cookies_file", "")
    monkeypatch.setattr(settings, "config_path", str(tmp_path))
    assert ytdlp.cookies_status()["configured"] is False

    ytdlp.save_cookies(
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tA\tB\n"
    )
    st = ytdlp.cookies_status()
    assert st["configured"] is True
    assert st["stale"] is False
    assert st["updated_at"]

    ytdlp.clear_cookies()
    assert ytdlp.cookies_status()["configured"] is False


def test_note_extraction_error_marks_stale_only_with_cookies(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "youtube_cookies_file", "")
    monkeypatch.setattr(settings, "config_path", str(tmp_path))

    # No cookies configured -> an age-gate error is 'missing', never 'stale'.
    ytdlp.note_extraction_error("ERROR: Sign in to confirm your age")
    assert ytdlp.cookies_status()["stale"] is False

    ytdlp.save_cookies("# Netscape\n.youtube.com\tTRUE\t/\tTRUE\t0\tA\tB\n")
    # A non-age-gate failure doesn't flag the cookies.
    ytdlp.note_extraction_error("ERROR: HTTP Error 404")
    assert ytdlp.cookies_status()["stale"] is False
    # An age-gate failure WITH cookies present means they likely expired.
    ytdlp.note_extraction_error("ERROR: Sign in to confirm your age")
    assert ytdlp.cookies_status()["stale"] is True
    # Re-saving clears the stale flag.
    ytdlp.save_cookies("# Netscape\n.youtube.com\tTRUE\t/\tTRUE\t0\tA\tC\n")
    assert ytdlp.cookies_status()["stale"] is False


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
