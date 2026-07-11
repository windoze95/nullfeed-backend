"""Provider seams for the discovery pipeline (embeddings + ranking).

Two providers can supply embeddings (gemini, openai — Anthropic has no
embeddings API) and four can supply the ranking LLM (anthropic, gemini,
openai, chatgpt); the two choices are independent and mix-and-match freely.
Gemini and OpenAI are called over plain REST via httpx so the base image
needs no extra SDKs; Anthropic reuses the already-installed ``anthropic``
package (imported lazily, like the other consumers).

The ``chatgpt`` rank provider is special: instead of an API key it uses a
ChatGPT Plus/Pro subscription via the Codex OAuth sign-in (see
``app/services/chatgpt_auth``) and calls the ChatGPT Codex backend — an
unofficial, best-effort surface that shares the plan's Codex usage limits.

Every network call lives behind a small module-level function so tests can
monkeypatch the seam instead of mocking SDKs or HTTP.
"""

import asyncio
import json
import logging

import httpx

from app.services import ai_config, chatgpt_auth

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
    # ChatGPT-subscription (Codex OAuth) — models the Codex backend accepts,
    # not api.openai.com ids.
    "chatgpt": "gpt-5.1-codex",
}

# Auto-detect order when no provider is configured explicitly. chatgpt is
# last: a completed sign-in is deliberate, but metered API keys are the more
# predictable default when both are present.
_EMBED_ORDER = ("gemini", "openai")
_RANK_ORDER = ("anthropic", "gemini", "openai", "chatgpt")

_TIMEOUT = 60.0
# Roomy for 10 picks with reasons, plus headroom for override models that
# spend part of the budget on thinking.
_MAX_RANK_TOKENS = 4096
# Gemini's batchEmbedContents caps the number of requests per call.
_GEMINI_EMBED_BATCH = 100

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_OPENAI_BASE = "https://api.openai.com/v1"
# The ChatGPT-plan Codex backend. OAuth tokens do NOT work against
# api.openai.com — the subscription path is only this endpoint.
_CHATGPT_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
# Streaming with reasoning can take a while; more generous than _TIMEOUT.
_CHATGPT_TIMEOUT = 120.0


def _provider_key(provider: str) -> str:
    if provider == "chatgpt":
        # No API key: "configured" means a completed ChatGPT sign-in.
        return "oauth" if chatgpt_auth.has_auth() else ""
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
    if provider == "chatgpt":
        return await _complete_chatgpt(prompt, model)
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


class _CodexUnauthorized(RuntimeError):
    """401 from the Codex backend — refresh once and retry."""


async def _complete_chatgpt(prompt: str, model: str) -> str:
    creds = await chatgpt_auth.get_access_credentials()
    if not creds:
        raise RuntimeError("chatgpt provider is not signed in (or needs re-auth)")
    try:
        return await _codex_responses(creds, prompt, model)
    except _CodexUnauthorized:
        creds = await chatgpt_auth.get_access_credentials(force_refresh=True)
        if not creds:
            raise RuntimeError("chatgpt provider re-auth required") from None
        try:
            return await _codex_responses(creds, prompt, model)
        except _CodexUnauthorized:
            # A freshly refreshed token still 401s — the Codex backend is
            # rejecting the account (e.g. Codex access revoked), not a stale
            # token. Surface a real message instead of the bare sentinel.
            raise RuntimeError(
                "Codex backend rejected a freshly refreshed token (401); "
                "the ChatGPT account may lack Codex access"
            ) from None


async def _codex_responses(creds: tuple[str, str], prompt: str, model: str) -> str:
    """One streamed call to the ChatGPT Codex backend; returns final text.

    The backend hard-requires ``store: false`` + ``stream: true`` and
    VALIDATES ``instructions`` — arbitrary text is rejected, so we send
    Codex's own base prompt and carry our real prompt as the user message
    (the same approach opencode and Hermes use).
    """
    from app.services.codex_instructions import CODEX_BASE_INSTRUCTIONS

    access_token, account_id = creds
    payload = {
        "model": model,
        "instructions": CODEX_BASE_INSTRUCTIONS,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        ],
        "store": False,
        "stream": True,
        "include": ["reasoning.encrypted_content"],
        # Ranking is easy; keep reasoning cheap so Discover barely dents the
        # plan's Codex quota. Codex models reject "none", so "low" it is.
        "reasoning": {"effort": "low", "summary": "auto"},
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": "codex_cli_rs",
        "accept": "text/event-stream",
    }
    parts: list[str] = []
    done = False
    # httpx's scalar timeout is per-read (it resets on every chunk / SSE
    # keep-alive comment), so a wedged-but-alive stream could hang forever
    # while holding the per-user discovery lock. Bound the whole exchange.
    async with asyncio.timeout(_CHATGPT_TIMEOUT):
        async with httpx.AsyncClient(timeout=_CHATGPT_TIMEOUT) as client:
            async with client.stream(
                "POST", _CHATGPT_RESPONSES_URL, headers=headers, json=payload
            ) as resp:
                if resp.status_code == 401:
                    raise _CodexUnauthorized()
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")[:300]
                    raise RuntimeError(f"Codex backend {resp.status_code}: {body}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        done = True
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    event_type = event.get("type")
                    if event_type == "response.output_text.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str):
                            parts.append(delta)
                    elif event_type in ("response.completed", "response.done"):
                        done = True
                        break
                    elif event_type in ("response.failed", "error"):
                        error = (event.get("response") or {}).get("error") or event
                        raise RuntimeError(
                            "Codex response failed: "
                            f"{error.get('code')} {error.get('message')}"
                        )
                    elif event_type == "response.incomplete":
                        reason = (event.get("response") or {}).get(
                            "incomplete_details"
                        ) or {}
                        raise RuntimeError(
                            f"Codex response incomplete: {reason.get('reason')}"
                        )
    if not done:
        # Stream ended with no terminal event — don't pass a truncated answer
        # off as complete.
        raise RuntimeError("Codex stream ended without a completion event")
    return "".join(parts)
