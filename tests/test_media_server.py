"""Media server range-request tests (design 1.4, RFC 7233)."""

import os

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request, Response
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.database import async_session_factory
from app.services.media_server import build_media_response
from tests.helpers import seed_channel, seed_video

pytestmark = pytest.mark.asyncio

CONTENT = b"0123456789" * 10  # 100 bytes


@pytest.fixture
def media_file(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(CONTENT)
    return str(path)


@pytest_asyncio.fixture
async def media_client(media_file):
    """A minimal app exercising build_media_response in isolation."""
    test_app = FastAPI()

    @test_app.get("/file")
    async def serve(request: Request) -> Response:
        return build_media_response(media_file, request.headers.get("range"))

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_full_file_200(media_client):
    resp = await media_client.get("/file")
    assert resp.status_code == 200
    assert resp.content == CONTENT
    assert resp.headers["content-length"] == str(len(CONTENT))
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["etag"]
    assert resp.headers["last-modified"]


async def test_open_ended_range(media_client):
    resp = await media_client.get("/file", headers={"Range": "bytes=0-"})
    assert resp.status_code == 206
    assert resp.content == CONTENT
    assert resp.headers["content-range"] == "bytes 0-99/100"
    assert resp.headers["etag"]
    assert resp.headers["last-modified"]


async def test_bounded_range(media_client):
    resp = await media_client.get("/file", headers={"Range": "bytes=10-19"})
    assert resp.status_code == 206
    assert resp.content == CONTENT[10:20]
    assert resp.headers["content-range"] == "bytes 10-19/100"
    assert resp.headers["content-length"] == "10"


async def test_suffix_range_returns_last_bytes(media_client):
    resp = await media_client.get("/file", headers={"Range": "bytes=-10"})
    assert resp.status_code == 206
    assert resp.content == CONTENT[-10:]
    assert resp.headers["content-range"] == "bytes 90-99/100"


async def test_range_end_clamped_to_file_size(media_client):
    resp = await media_client.get("/file", headers={"Range": "bytes=90-200"})
    assert resp.status_code == 206
    assert resp.content == CONTENT[90:]
    assert resp.headers["content-range"] == "bytes 90-99/100"


async def test_multi_range_serves_first_range_only(media_client):
    resp = await media_client.get("/file", headers={"Range": "bytes=0-9,20-29"})
    assert resp.status_code == 206
    assert resp.content == CONTENT[0:10]
    assert resp.headers["content-range"] == "bytes 0-9/100"


@pytest.mark.parametrize(
    "range_header",
    [
        "bytes=100-",  # start >= size
        "bytes=abc-",  # non-numeric
        "items=0-10",  # wrong unit
        "bytes=5-2",  # start > end
        "bytes=-0",  # zero-length suffix
        "bytes=",  # empty spec
    ],
)
async def test_unsatisfiable_or_malformed_range_416(media_client, range_header):
    resp = await media_client.get("/file", headers={"Range": range_header})
    assert resp.status_code == 416
    assert resp.headers["content-range"] == "bytes */100"


async def test_stream_endpoint_auth_and_ranges(client, make_user):
    _, headers = await make_user()
    token = headers["X-User-Token"]

    rel_path = "chan/streamed.mp4"
    full_path = os.path.join(settings.media_path, rel_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "wb") as f:
        f.write(CONTENT)

    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="COMPLETE", file_path=rel_path)

    # Missing/garbage tokens are rejected.
    resp = await client.get(f"/api/videos/{video.id}/stream")
    assert resp.status_code == 401
    resp = await client.get(f"/api/videos/{video.id}/stream?token=garbage")
    assert resp.status_code == 401

    # Token via query param (used by video elements/players).
    resp = await client.get(f"/api/videos/{video.id}/stream?token={token}")
    assert resp.status_code == 200
    assert resp.content == CONTENT

    resp = await client.get(
        f"/api/videos/{video.id}/stream?token={token}",
        headers={"Range": "bytes=10-19"},
    )
    assert resp.status_code == 206
    assert resp.content == CONTENT[10:20]
    assert resp.headers["content-range"] == "bytes 10-19/100"
