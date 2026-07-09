"""Instant-start playback: progressive-URL resolve cache + reverse proxy (#85)."""

import asyncio
import os

import httpx
import pytest

import app.api.videos as videos_api
import app.services.instant_stream as instant_stream
from app.config import settings
from app.database import async_session_factory
from app.services.instant_stream import InstantStreamError
from tests.helpers import seed_channel, seed_video

pytestmark = pytest.mark.asyncio


def _mock_client_factory(body: bytes):
    """An httpx.AsyncClient whose MockTransport serves ``body`` with Range support.

    The body is returned as an async byte stream (not buffered ``content=``) so
    the response is a genuine stream — exactly what the proxy's ``aiter_raw``
    consumes against a real transport.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        rng = request.headers.get("range")
        if rng and rng.startswith("bytes="):
            spec = rng[len("bytes=") :].split(",")[0]
            start_s, _, end_s = spec.partition("-")
            start = int(start_s)
            end = int(end_s) if end_s else len(body) - 1
            chunk = body[start : end + 1]

            async def range_stream():
                yield chunk

            return httpx.Response(
                206,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{len(body)}",
                    "Content-Length": str(len(chunk)),
                    "Content-Type": "video/mp4",
                },
                content=range_stream(),
            )

        async def full_stream():
            yield body

        return httpx.Response(
            200,
            headers={"Content-Length": str(len(body)), "Content-Type": "video/mp4"},
            content=full_stream(),
        )

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- resolve cache --------------------------------------------------------


async def test_resolve_caches_url(monkeypatch):
    calls = []

    def fake_get(vid: str) -> str:
        calls.append(vid)
        return "https://upstream.test/v.mp4?expire=9999999999"

    monkeypatch.setattr(instant_stream, "_ytdlp_get_url", fake_get)

    first = instant_stream.resolve_progressive_url("vid-1")
    second = instant_stream.resolve_progressive_url("vid-1")

    assert first == second
    assert calls == ["vid-1"]  # resolved once, second call hit the cache


async def test_resolve_failure_raises(monkeypatch):
    def boom(vid: str) -> str:
        raise InstantStreamError("nope")

    monkeypatch.setattr(instant_stream, "_ytdlp_get_url", boom)
    with pytest.raises(InstantStreamError):
        instant_stream.resolve_progressive_url("vid-2")


# --- stream_proxy Accept-Ranges semantics ----------------------------------


async def _drain(resp) -> bytes:
    """Consume a StreamingResponse body (also tears down the proxy's client)."""
    return b"".join([chunk async for chunk in resp.body_iterator])


async def test_stream_proxy_206_advertises_ranges(monkeypatch):
    body = bytes(i % 256 for i in range(1000))
    monkeypatch.setattr(instant_stream, "_make_client", _mock_client_factory(body))

    resp = await instant_stream.stream_proxy(
        "https://upstream.test/v.mp4", "bytes=0-99"
    )

    assert resp.status_code == 206
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["content-range"] == "bytes 0-99/1000"
    assert await _drain(resp) == body[:100]


async def test_stream_proxy_200_fallback_does_not_advertise_ranges(monkeypatch):
    """An upstream that ignores Range and sends the full body must not leave
    the player believing seeks work — Accept-Ranges flips to ``none`` and no
    Content-Range leaks through."""
    body = b"x" * 512

    def ignores_range(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("range") == "bytes=0-99"  # forwarded, ignored

        async def full_stream():
            yield body

        return httpx.Response(
            200,
            headers={
                "Content-Length": str(len(body)),
                "Content-Type": "video/mp4",
                # Upstream *claims* range support while ignoring the request;
                # the demonstrated behavior must win over the claim.
                "Accept-Ranges": "bytes",
            },
            content=full_stream(),
        )

    monkeypatch.setattr(
        instant_stream,
        "_make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(ignores_range)),
    )

    resp = await instant_stream.stream_proxy(
        "https://upstream.test/v.mp4", "bytes=0-99"
    )

    assert resp.status_code == 200
    assert resp.headers["accept-ranges"] == "none"
    assert "content-range" not in resp.headers
    assert await _drain(resp) == body


async def test_stream_proxy_plain_get_forwards_upstream_accept_ranges(monkeypatch):
    """No client Range: a 200 proves nothing, so upstream's own Accept-Ranges
    passes through when present and stays absent when it isn't."""
    body = b"y" * 64

    def with_claim(request: httpx.Request) -> httpx.Response:
        async def full_stream():
            yield body

        return httpx.Response(
            200,
            headers={"Content-Type": "video/mp4", "Accept-Ranges": "bytes"},
            content=full_stream(),
        )

    monkeypatch.setattr(
        instant_stream,
        "_make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(with_claim)),
    )
    resp = await instant_stream.stream_proxy("https://upstream.test/v.mp4", None)
    assert resp.status_code == 200
    assert resp.headers["accept-ranges"] == "bytes"
    await _drain(resp)

    # _mock_client_factory's plain-200 handler sends no Accept-Ranges at all.
    monkeypatch.setattr(instant_stream, "_make_client", _mock_client_factory(body))
    resp = await instant_stream.stream_proxy("https://upstream.test/v.mp4", None)
    assert resp.status_code == 200
    assert "accept-ranges" not in resp.headers
    await _drain(resp)


# --- endpoint -------------------------------------------------------------


async def test_instant_stream_requires_auth(client):
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="CATALOGED")

    resp = await client.get(f"/api/videos/{video.id}/instant-stream")
    assert resp.status_code == 401


async def test_instant_stream_unknown_video_404(client, make_user):
    _, headers = await make_user()
    resp = await client.get(
        "/api/videos/does-not-exist/instant-stream", headers=headers
    )
    assert resp.status_code == 404


async def test_instant_stream_proxies_source(client, make_user, monkeypatch):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="CATALOGED")

    body = b"".join(bytes([i % 256]) for i in range(1000))
    monkeypatch.setattr(
        videos_api, "resolve_progressive_url", lambda vid: "https://upstream.test/v.mp4"
    )
    monkeypatch.setattr(instant_stream, "_make_client", _mock_client_factory(body))

    # Full stream.
    resp = await client.get(f"/api/videos/{video.id}/instant-stream", headers=headers)
    assert resp.status_code == 200
    assert resp.content == body

    # Range request is forwarded and the 206 + Content-Range pass through.
    resp = await client.get(
        f"/api/videos/{video.id}/instant-stream",
        headers={**headers, "Range": "bytes=0-99"},
    )
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == "bytes 0-99/1000"
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert resp.content == body[:100]


async def test_instant_stream_resolve_failure_502(client, make_user, monkeypatch):
    user, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="CATALOGED")

    def boom(vid: str) -> str:
        raise InstantStreamError("resolve failed")

    monkeypatch.setattr(videos_api, "resolve_progressive_url", boom)

    resp = await client.get(f"/api/videos/{video.id}/instant-stream", headers=headers)
    assert resp.status_code == 502


async def test_instant_stream_first_byte_timeout_502(client, make_user, monkeypatch):
    """A source that accepts the connection then stalls before responding is a
    502 (so the client falls back), not an indefinite hang on the spinner."""
    _, headers = await make_user()
    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="CATALOGED")

    monkeypatch.setattr(
        videos_api, "resolve_progressive_url", lambda vid: "https://upstream.test/v.mp4"
    )
    monkeypatch.setattr(instant_stream, "_FIRST_BYTE_TIMEOUT_SECONDS", 0.05)

    async def stalled(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)  # never sends within the first-byte budget
        return httpx.Response(200, content=b"")

    monkeypatch.setattr(
        instant_stream,
        "_make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(stalled)),
    )

    resp = await client.get(f"/api/videos/{video.id}/instant-stream", headers=headers)
    assert resp.status_code == 502


async def test_instant_stream_serves_complete_file_without_resolving(
    client, make_user, monkeypatch
):
    """A COMPLETE video is served from disk; the source is never resolved."""
    user, headers = await make_user()

    os.makedirs(settings.media_path, exist_ok=True)
    rel_path = "instant_complete.mp4"
    body = b"local-hq-bytes"
    with open(os.path.join(settings.media_path, rel_path), "wb") as f:
        f.write(body)

    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="COMPLETE", file_path=rel_path)

    def fail(vid: str) -> str:
        raise AssertionError("resolve must not be called for a COMPLETE video")

    monkeypatch.setattr(videos_api, "resolve_progressive_url", fail)

    resp = await client.get(f"/api/videos/{video.id}/instant-stream", headers=headers)
    assert resp.status_code == 200
    assert resp.content == body
