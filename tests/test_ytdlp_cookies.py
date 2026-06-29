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


def test_normalize_prepends_missing_header():
    raw = ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc"
    out = ytdlp.normalize_cookies(raw)
    assert out.startswith("# Netscape HTTP Cookie File\n")
    assert ytdlp.has_cookie_rows(out)


def test_normalize_keeps_existing_header_and_strips_bom():
    raw = "﻿# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tA\tB"
    out = ytdlp.normalize_cookies(raw)
    assert out.count("# Netscape HTTP Cookie File") == 1
    assert out.startswith("# Netscape HTTP Cookie File\n")


def test_has_cookie_rows_rejects_non_cookies():
    assert ytdlp.has_cookie_rows("just some words\nno tabs here") is False
    assert ytdlp.has_cookie_rows("# Netscape HTTP Cookie File\n") is False
    assert ytdlp.has_cookie_rows(".youtube.com\tTRUE\t/\tTRUE\t0\tA\tB") is True


def test_status_surfaces_last_error(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "youtube_cookies_file", "")
    monkeypatch.setattr(settings, "config_path", str(tmp_path))
    ytdlp.save_cookies(".youtube.com\tTRUE\t/\tTRUE\t0\tA\tB")
    assert ytdlp.cookies_status()["stale"] is False
    ytdlp.note_extraction_error(
        "ERROR: 'cookies.txt' does not look like a Netscape format cookies file"
    )
    st = ytdlp.cookies_status()
    assert st["stale"] is True
    assert "Netscape" in (st["last_error"] or "")
    # Re-saving clears the error.
    ytdlp.save_cookies(".youtube.com\tTRUE\t/\tTRUE\t0\tA\tC")
    assert ytdlp.cookies_status()["stale"] is False


def test_normalize_repairs_spaces_to_tabs():
    raw = "# Netscape HTTP Cookie File\n.youtube.com    TRUE    /    TRUE    0    SID    abc"
    out = ytdlp.normalize_cookies(raw)
    row = next(ln for ln in out.splitlines() if ln.startswith(".youtube.com"))
    assert row.count("\t") == 6  # 7 fields, tab-separated
    assert ytdlp.has_cookie_rows(out)


def test_verify_cookies_probes_age_restricted(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "youtube_cookies_file", "")
    monkeypatch.setattr(settings, "config_path", str(tmp_path))
    ytdlp.save_cookies(".youtube.com\tTRUE\t/\tTRUE\t0\tA\tB")

    # Age-restricted probe resolves → fully working.
    monkeypatch.setattr(ytdlp, "_probe_error", lambda vid: None)
    assert ytdlp.verify_cookies() is None

    # Age gate on the age probe → "doesn't unlock age-restricted".
    monkeypatch.setattr(
        ytdlp,
        "_probe_error",
        lambda vid: (
            "ERROR: Sign in to confirm your age"
            if vid == ytdlp._AGE_PROBE_VIDEO_ID
            else None
        ),
    )
    msg = ytdlp.verify_cookies()
    assert msg is not None and "age-restricted" in msg

    # Malformed file → surfaced verbatim.
    monkeypatch.setattr(
        ytdlp,
        "_probe_error",
        lambda vid: "ERROR: does not look like a Netscape format cookies file",
    )
    msg = ytdlp.verify_cookies()
    assert msg is not None and "Netscape" in msg

    # Age probe unavailable (probe video removed) but normal session fine → ok.
    monkeypatch.setattr(
        ytdlp,
        "_probe_error",
        lambda vid: (
            "ERROR: Video unavailable" if vid == ytdlp._AGE_PROBE_VIDEO_ID else None
        ),
    )
    assert ytdlp.verify_cookies() is None

    # A format/playback error (not auth) must NOT mark valid cookies broken —
    # whether a progressive stream exists is a playback concern, not the cookies'.
    monkeypatch.setattr(
        ytdlp,
        "_probe_error",
        lambda vid: (
            "ERROR: Requested format is not available"
            if vid == ytdlp._AGE_PROBE_VIDEO_ID
            else None
        ),
    )
    assert ytdlp.verify_cookies() is None


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
    # Forces a progressive-capable player client (cookie-auth web is SABR-only).
    assert "youtube:player_client=android,web" in captured["cmd"]
