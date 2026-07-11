"""Provider REST layer: request shapes and response parsing over mock HTTP."""

import json

import httpx
import pytest

import app.services.llm_providers as llm_providers
from app.config import settings

pytestmark = pytest.mark.asyncio


def _mock_http(monkeypatch, handler):
    """Route the module's httpx.AsyncClient through a MockTransport."""
    real_client = httpx.AsyncClient

    def factory(*_args, **_kwargs):
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(llm_providers.httpx, "AsyncClient", factory)


async def test_embed_openai_orders_by_index_and_uses_bearer(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        # Deliberately out of order: the client must sort by index.
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [3.0, 4.0]},
                    {"index": 0, "embedding": [1.0, 2.0]},
                ]
            },
        )

    _mock_http(monkeypatch, handler)
    vectors = await llm_providers._embed_openai(["a", "b"], "text-embedding-3-small")
    assert vectors == [[1.0, 2.0], [3.0, 4.0]]
    assert seen["auth"] == "Bearer sk-test"


async def test_embed_gemini_batches_and_keeps_key_out_of_url(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "g-test")
    requests = []
    counter = {"n": 0}

    def handler(request):
        payload = json.loads(request.content)
        requests.append(
            {
                "url": str(request.url),
                "header_key": request.headers.get("x-goog-api-key"),
                "batch_size": len(payload["requests"]),
            }
        )
        embeddings = []
        for _ in payload["requests"]:
            embeddings.append({"values": [float(counter["n"])]})
            counter["n"] += 1
        return httpx.Response(200, json={"embeddings": embeddings})

    _mock_http(monkeypatch, handler)
    texts = [f"text {i}" for i in range(120)]
    vectors = await llm_providers._embed_gemini(texts, "gemini-embedding-2")

    # 120 texts split into a 100-batch and a 20-batch, order preserved.
    assert [r["batch_size"] for r in requests] == [100, 20]
    assert vectors == [[float(i)] for i in range(120)]
    for r in requests:
        assert r["header_key"] == "g-test"
        # The key must never ride the URL — httpx logs full request URLs.
        assert "g-test" not in r["url"]
        assert "key=" not in r["url"]


async def test_complete_gemini_joins_parts(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "g-test")

    def handler(request):
        assert request.headers.get("x-goog-api-key") == "g-test"
        assert "key=" not in str(request.url)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": '["a", '}, {"text": '"b"]'}]}}
                ]
            },
        )

    _mock_http(monkeypatch, handler)
    text = await llm_providers._complete_gemini("prompt", "gemini-3.5-flash")
    assert text == '["a", "b"]'


async def test_complete_openai_reads_message_content(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")

    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "gpt-5.6-luna"
        assert body["messages"][0]["role"] == "user"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '["query one"]'}}]},
        )

    _mock_http(monkeypatch, handler)
    text = await llm_providers._complete_openai("prompt", "gpt-5.6-luna")
    assert text == '["query one"]'


async def test_embed_provider_error_propagates(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")

    def handler(request):
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    _mock_http(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        await llm_providers._embed_openai(["a"], "text-embedding-3-small")
