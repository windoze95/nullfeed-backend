"""Provider seams for the discovery pipeline (embeddings + ranking).

Two providers can supply embeddings (gemini, openai — Anthropic has no
embeddings API) and three can supply the ranking LLM (anthropic, gemini,
openai); the two choices are independent and mix-and-match freely. Gemini
and OpenAI are called over plain REST via httpx so the base image needs no
extra SDKs; Anthropic reuses the already-installed ``anthropic`` package
(imported lazily, like the other consumers).

Every network call lives behind a small module-level function so tests can
monkeypatch the seam instead of mocking SDKs or HTTP.
"""

import logging

import httpx

from app.services import ai_config

logger = logging.getLogger(__name__)

# Provider-default models, overridable via NULLFEED_EMBED_MODEL /
# NULLFEED_RANK_MODEL for when a vendor retires a default between releases.
EMBED_MODELS = {
    "gemini": "gemini-embedding-2",
    "openai": "text-embedding-3-small",
}
RANK_MODELS = {
    "anthropic": "claude-haiku-4-5",
    "gemini": "gemini-3.5-flash",
    "openai": "gpt-5.6-luna",
}

# Auto-detect order when no provider is configured explicitly.
_EMBED_ORDER = ("gemini", "openai")
_RANK_ORDER = ("anthropic", "gemini", "openai")

_TIMEOUT = 60.0
# Roomy for 10 picks with reasons, plus headroom for override models that
# spend part of the budget on thinking.
_MAX_RANK_TOKENS = 4096
# Gemini's batchEmbedContents caps the number of requests per call.
_GEMINI_EMBED_BATCH = 100

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_OPENAI_BASE = "https://api.openai.com/v1"


def _provider_key(provider: str) -> str:
    return ai_config.get_key(provider)


def _resolve(
    explicit: str, model_override: str, defaults: dict[str, str], order: tuple
) -> tuple[str, str] | None:
    explicit = explicit.strip().lower()
    if explicit:
        if explicit not in defaults:
            logger.warning("Unknown discovery provider %r; feature disabled", explicit)
            return None
        if not _provider_key(explicit):
            logger.warning(
                "Discovery provider %r configured but its API key is missing; "
                "feature disabled",
                explicit,
            )
            return None
        return explicit, (model_override or defaults[explicit])
    if model_override:
        # Auto-detect could pair the override with a different vendor than
        # the user meant (e.g. an OpenAI model name against the Gemini API),
        # failing every call. Overrides require an explicit provider.
        logger.warning(
            "Model override %r ignored: set the provider explicitly to use it",
            model_override,
        )
    for provider in order:
        if _provider_key(provider):
            return provider, defaults[provider]
    return None


def resolve_embed_provider() -> tuple[str, str] | None:
    """Return (provider, model) for embeddings, or None when unavailable."""
    provider, model = ai_config.get_embed_selection()
    return _resolve(provider, model, EMBED_MODELS, _EMBED_ORDER)


def resolve_rank_provider() -> tuple[str, str] | None:
    """Return (provider, model) for ranking, or None when unavailable."""
    provider, model = ai_config.get_rank_selection()
    return _resolve(provider, model, RANK_MODELS, _RANK_ORDER)


def embed_model_key() -> str | None:
    """The "provider:model" string keying rows in channel_embeddings."""
    resolved = resolve_embed_provider()
    if not resolved:
        return None
    return f"{resolved[0]}:{resolved[1]}"


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts with the configured provider, in input order.

    Raises on provider failure — callers treat discovery as best-effort and
    degrade to no recommendations.
    """
    resolved = resolve_embed_provider()
    if not resolved:
        raise RuntimeError("No embedding provider configured")
    provider, model = resolved
    if provider == "gemini":
        return await _embed_gemini(texts, model)
    return await _embed_openai(texts, model)


async def rank_complete(prompt: str) -> str:
    """Single-prompt completion with the configured ranking provider."""
    resolved = resolve_rank_provider()
    if not resolved:
        raise RuntimeError("No ranking provider configured")
    provider, model = resolved
    if provider == "anthropic":
        return await _complete_anthropic(prompt, model)
    if provider == "gemini":
        return await _complete_gemini(prompt, model)
    return await _complete_openai(prompt, model)


async def _embed_gemini(texts: list[str], model: str) -> list[list[float]]:
    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for start in range(0, len(texts), _GEMINI_EMBED_BATCH):
            batch = texts[start : start + _GEMINI_EMBED_BATCH]
            resp = await client.post(
                f"{_GEMINI_BASE}/{model}:batchEmbedContents",
                # Header, NOT a ?key= query param: httpx logs full request
                # URLs at INFO, which would leak the key into app logs.
                headers={"x-goog-api-key": ai_config.get_key("gemini")},
                json={
                    "requests": [
                        {
                            "model": f"models/{model}",
                            "content": {"parts": [{"text": text}]},
                        }
                        for text in batch
                    ]
                },
            )
            resp.raise_for_status()
            vectors.extend(e["values"] for e in resp.json()["embeddings"])
    return vectors


async def _embed_openai(texts: list[str], model: str) -> list[list[float]]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{_OPENAI_BASE}/embeddings",
            headers={"Authorization": f"Bearer {ai_config.get_key('openai')}"},
            json={"model": model, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()
    items = sorted(data["data"], key=lambda d: d["index"])
    return [d["embedding"] for d in items]


async def _complete_anthropic(prompt: str, model: str) -> str:
    import anthropic  # lazy: keep the dependency optional at runtime

    client = anthropic.AsyncAnthropic(api_key=ai_config.get_key("anthropic"))
    message = await client.messages.create(
        model=model,
        max_tokens=_MAX_RANK_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    # Scan for the first text block: models with thinking enabled (possible
    # via the NULLFEED_RANK_MODEL override) put a thinking block first.
    for block in message.content:
        if getattr(block, "type", "") == "text" and hasattr(block, "text"):
            return block.text
    return ""


async def _complete_gemini(prompt: str, model: str) -> str:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{_GEMINI_BASE}/{model}:generateContent",
            headers={"x-goog-api-key": ai_config.get_key("gemini")},
            json={"contents": [{"parts": [{"text": prompt}]}]},
        )
        resp.raise_for_status()
        data = resp.json()
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts)


async def _complete_openai(prompt: str, model: str) -> str:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{_OPENAI_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {ai_config.get_key('openai')}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        data = resp.json()
    return data["choices"][0]["message"]["content"]
