"""ChatGPT (Codex OAuth) sign-in + the chatgpt rank provider."""

import base64
import json
import stat
import time

import httpx
import pytest

import app.services.chatgpt_auth as chatgpt_auth
import app.services.llm_providers as llm_providers
from app.config import settings

pytestmark = pytest.mark.asyncio


def _jwt(claims: dict) -> str:
    """Unsigned JWT-shaped token: header.payload.signature."""

    def seg(data: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
        return raw.rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


def _access_token(expires_in: float = 3600, account_id: str = "acct-123") -> str:
    return _jwt(
        {
            "exp": time.time() + expires_in,
            "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
        }
    )


def _seed_auth(expires_in: float = 3600, refresh_token: str = "rt-1") -> None:
    chatgpt_auth._persist_tokens(
        {
            "access_token": _access_token(expires_in),
            "refresh_token": refresh_token,
            "id_token": _jwt({}),
        }
    )


def _mock_http(monkeypatch, module, handler):
    real_client = httpx.AsyncClient

    def factory(*_args, **_kwargs):
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(module.httpx, "AsyncClient", factory)


def _set_no_keys(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "discovery_embed_provider", "")
    monkeypatch.setattr(settings, "discovery_rank_provider", "")
    monkeypatch.setattr(settings, "discovery_embed_model", "")
    monkeypatch.setattr(settings, "discovery_rank_model", "")


# --- device flow -------------------------------------------------------------


async def test_device_flow_end_to_end(monkeypatch):
    polls = {"n": 0}

    def handler(request):
        url = str(request.url)
        if url.endswith("/api/accounts/deviceauth/usercode"):
            assert json.loads(request.content) == {"client_id": chatgpt_auth.CLIENT_ID}
            return httpx.Response(
                200,
                json={
                    "device_auth_id": "dev-1",
                    "usercode": "ABCD-1234",  # serde alias form
                    "interval": 3,
                },
            )
        if url.endswith("/api/accounts/deviceauth/token"):
            polls["n"] += 1
            if polls["n"] == 1:
                return httpx.Response(403)  # not approved yet
            return httpx.Response(
                200,
                json={"authorization_code": "code-1", "code_verifier": "ver-1"},
            )
        if url.endswith("/oauth/token"):
            body = dict(
                pair.split("=", 1)
                for pair in request.content.decode().split("&")
                if "=" in pair
            )
            assert body["grant_type"] == "authorization_code"
            assert body["code"] == "code-1"
            assert body["code_verifier"] == "ver-1"
            return httpx.Response(
                200,
                json={
                    "access_token": _access_token(),
                    "refresh_token": "rt-fresh",
                    "id_token": _jwt({}),
                },
            )
        raise AssertionError(f"unexpected URL {url}")

    _mock_http(monkeypatch, chatgpt_auth, handler)

    started = await chatgpt_auth.start_device_login()
    assert started["user_code"] == "ABCD-1234"
    assert started["verification_url"].endswith("/codex/device")
    assert chatgpt_auth.auth_status()["pending"] is True

    first = await chatgpt_auth.poll_device_login()
    assert first["status"] == "pending"

    second = await chatgpt_auth.poll_device_login()
    assert second["status"] == "connected"
    assert second["account_id"] == "acct-123"

    status = chatgpt_auth.auth_status()
    assert status["connected"] is True
    assert status["pending"] is False
    assert chatgpt_auth.has_auth() is True

    mode = stat.S_IMODE(chatgpt_auth._auth_path().stat().st_mode)
    assert mode == 0o600


async def test_start_device_login_surfaces_disabled_device_auth(monkeypatch):
    def handler(request):
        return httpx.Response(403, json={"error": "forbidden"})

    _mock_http(monkeypatch, chatgpt_auth, handler)
    with pytest.raises(chatgpt_auth.DeviceLoginError) as excinfo:
        await chatgpt_auth.start_device_login()
    assert "enable it in ChatGPT Settings" in str(excinfo.value)


async def test_poll_without_pending_flow_is_idle():
    assert (await chatgpt_auth.poll_device_login()) == {"status": "idle"}


# --- refresh -----------------------------------------------------------------


async def test_refresh_rotates_token_when_expired(monkeypatch):
    _seed_auth(expires_in=-10, refresh_token="rt-old")
    calls = {"n": 0}

    def handler(request):
        assert str(request.url).endswith("/oauth/token")
        body = json.loads(request.content)  # JSON body, matching codex-rs
        assert body == {
            "client_id": chatgpt_auth.CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": "rt-old",
        }
        calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "access_token": _access_token(account_id="acct-123"),
                "refresh_token": "rt-rotated",
            },
        )

    _mock_http(monkeypatch, chatgpt_auth, handler)
    creds = await chatgpt_auth.get_access_credentials()
    assert creds is not None
    _token, account_id = creds
    assert account_id == "acct-123"
    assert calls["n"] == 1

    # The rotated (single-use) refresh token must be persisted.
    record = chatgpt_auth._load(chatgpt_auth._auth_path())
    assert record["refresh_token"] == "rt-rotated"

    # A fresh token skips the network entirely.
    creds = await chatgpt_auth.get_access_credentials()
    assert creds is not None
    assert calls["n"] == 1


async def test_refresh_invalid_grant_marks_needs_reauth(monkeypatch):
    _seed_auth(expires_in=-10)

    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    _mock_http(monkeypatch, chatgpt_auth, handler)
    assert (await chatgpt_auth.get_access_credentials()) is None
    status = chatgpt_auth.auth_status()
    assert status["needs_reauth"] is True
    # A broken credential no longer counts as a configured provider.
    assert chatgpt_auth.has_auth() is False


async def test_transient_refresh_failure_does_not_require_reauth(monkeypatch):
    _seed_auth(expires_in=-10)

    def handler(request):
        return httpx.Response(500)

    _mock_http(monkeypatch, chatgpt_auth, handler)
    assert (await chatgpt_auth.get_access_credentials()) is None
    assert chatgpt_auth.auth_status()["needs_reauth"] is False
    assert chatgpt_auth.has_auth() is True


# --- provider resolution -----------------------------------------------------


async def test_chatgpt_resolves_last_in_auto_order(monkeypatch):
    _set_no_keys(monkeypatch)
    assert llm_providers.resolve_rank_provider() is None

    _seed_auth()
    assert llm_providers.resolve_rank_provider() == ("chatgpt", "gpt-5.1-codex")
    # Embeddings can never come from the ChatGPT surface.
    assert llm_providers.resolve_embed_provider() is None

    # A metered key outranks the subscription in auto mode.
    monkeypatch.setattr(settings, "anthropic_api_key", "a-x")
    assert llm_providers.resolve_rank_provider() == ("anthropic", "claude-haiku-4-5")

    # ...but an explicit selection pins it.
    monkeypatch.setattr(settings, "discovery_rank_provider", "chatgpt")
    assert llm_providers.resolve_rank_provider() == ("chatgpt", "gpt-5.1-codex")

    # Explicit chatgpt for EMBEDDINGS is invalid.
    monkeypatch.setattr(settings, "discovery_embed_provider", "chatgpt")
    assert llm_providers.resolve_embed_provider() is None


async def test_rank_complete_dispatches_to_chatgpt(monkeypatch):
    """The resolution -> dispatch wiring, not just the two pieces alone."""
    _set_no_keys(monkeypatch)
    _seed_auth()
    monkeypatch.setattr(settings, "discovery_rank_provider", "chatgpt")

    captured = {}

    async def fake_complete(prompt, model):
        captured["prompt"] = prompt
        captured["model"] = model
        return "dispatched"

    monkeypatch.setattr(llm_providers, "_complete_chatgpt", fake_complete)
    result = await llm_providers.rank_complete("rank this")
    assert result == "dispatched"
    assert captured == {"prompt": "rank this", "model": "gpt-5.1-codex"}


async def test_refresh_ignores_concurrent_rotation(monkeypatch):
    """A refresh whose token was rotated by another worker mid-call must not
    clobber the store with its stale record."""
    _seed_auth(expires_in=-10, refresh_token="rt-old")

    async def fake_post(*_args, **kwargs):
        # Simulate a peer worker rotating the credential while our POST is in
        # flight: the on-disk store now holds a fresh token under rt-new.
        chatgpt_auth._persist_tokens(
            {
                "access_token": _access_token(),
                "refresh_token": "rt-new",
            }
        )
        return httpx.Response(400, json={"error": "invalid_grant"})

    async def fake_aenter(self):
        return self

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "__aenter__", fake_aenter)

    creds = await chatgpt_auth.get_access_credentials()
    # We used the peer's fresh credentials instead of marking needs_reauth.
    assert creds is not None
    record = chatgpt_auth._load(chatgpt_auth._auth_path())
    assert record["refresh_token"] == "rt-new"
    assert record.get("needs_reauth") is not True
    assert chatgpt_auth.has_auth() is True


async def test_refresh_empty_body_is_transient_not_reauth(monkeypatch):
    """A 403 with no error code (WAF/CDN blip) must NOT brick the sign-in."""
    _seed_auth(expires_in=-10)

    def handler(request):
        return httpx.Response(403, text="")

    _mock_http(monkeypatch, chatgpt_auth, handler)
    assert (await chatgpt_auth.get_access_credentials()) is None
    assert chatgpt_auth.auth_status()["needs_reauth"] is False
    assert chatgpt_auth.has_auth() is True


# --- the completion call -----------------------------------------------------


def _sse(events: list[dict | str]) -> bytes:
    lines = []
    for event in events:
        data = event if isinstance(event, str) else json.dumps(event)
        lines.append(f"data: {data}\n\n")
    return "".join(lines).encode()


async def test_complete_chatgpt_accumulates_deltas(monkeypatch):
    _seed_auth()
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=_sse(
                [
                    {"type": "response.created"},
                    {"type": "response.output_text.delta", "delta": '["a"'},
                    {"type": "response.reasoning_text.delta", "delta": "hmm"},
                    {"type": "response.output_text.delta", "delta": ", 2]"},
                    {"type": "response.completed", "response": {"id": "r1"}},
                    "[DONE]",
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )

    _mock_http(monkeypatch, llm_providers, handler)
    text = await llm_providers._complete_chatgpt("rank these", "gpt-5.1-codex")
    assert text == '["a", 2]'

    assert seen["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert seen["headers"]["chatgpt-account-id"] == "acct-123"
    assert seen["headers"]["originator"] == "codex_cli_rs"
    assert seen["headers"]["openai-beta"] == "responses=experimental"

    payload = seen["payload"]
    assert payload["store"] is False
    assert payload["stream"] is True
    assert payload["model"] == "gpt-5.1-codex"
    # The validated instructions are Codex's own base prompt; ours rides as
    # the user input item.
    assert payload["instructions"].startswith("You are Codex")
    assert payload["input"][0]["content"][0]["text"] == "rank these"


async def test_complete_chatgpt_retries_once_after_401(monkeypatch):
    _seed_auth(refresh_token="rt-old")
    calls = {"responses": 0, "refresh": 0}

    def handler(request):
        url = str(request.url)
        if url.endswith("/oauth/token"):
            calls["refresh"] += 1
            return httpx.Response(
                200,
                json={
                    "access_token": _access_token(),
                    "refresh_token": "rt-new",
                },
            )
        calls["responses"] += 1
        if calls["responses"] == 1:
            return httpx.Response(401)
        return httpx.Response(
            200,
            content=_sse(
                [
                    {"type": "response.output_text.delta", "delta": "ok"},
                    {"type": "response.done"},
                    "[DONE]",
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )

    # Both modules share the global httpx module, so one patch covers the
    # responses call AND the refresh call.
    _mock_http(monkeypatch, llm_providers, handler)
    text = await llm_providers._complete_chatgpt("prompt", "gpt-5.1-codex")
    assert text == "ok"
    assert calls == {"responses": 2, "refresh": 1}


async def test_complete_chatgpt_surfaces_usage_limit(monkeypatch):
    _seed_auth()

    def handler(request):
        return httpx.Response(
            429,
            json={"error": {"type": "usage_limit_reached", "resets_at": 1234}},
        )

    _mock_http(monkeypatch, llm_providers, handler)
    with pytest.raises(RuntimeError) as excinfo:
        await llm_providers._complete_chatgpt("prompt", "gpt-5.1-codex")
    assert "429" in str(excinfo.value)
    assert "usage_limit_reached" in str(excinfo.value)


async def test_complete_chatgpt_stream_without_terminal_event(monkeypatch):
    _seed_auth()

    def handler(request):
        # Deltas but no completed/done/[DONE] — a truncated stream.
        return httpx.Response(
            200,
            content=_sse([{"type": "response.output_text.delta", "delta": "partial"}]),
            headers={"content-type": "text/event-stream"},
        )

    _mock_http(monkeypatch, llm_providers, handler)
    with pytest.raises(RuntimeError) as excinfo:
        await llm_providers._complete_chatgpt("prompt", "gpt-5.1-codex")
    assert "without a completion event" in str(excinfo.value)


async def test_complete_chatgpt_persistent_401_raises_descriptive(monkeypatch):
    _seed_auth(refresh_token="rt-old")

    def handler(request):
        if str(request.url).endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={"access_token": _access_token(), "refresh_token": "rt-new"},
            )
        return httpx.Response(401)  # both response calls 401

    _mock_http(monkeypatch, llm_providers, handler)
    with pytest.raises(RuntimeError) as excinfo:
        await llm_providers._complete_chatgpt("prompt", "gpt-5.1-codex")
    # A real message, not the empty bare-sentinel str.
    assert "Codex" in str(excinfo.value) and str(excinfo.value)


async def test_complete_chatgpt_response_failed_event(monkeypatch):
    _seed_auth()

    def handler(request):
        return httpx.Response(
            200,
            content=_sse(
                [
                    {
                        "type": "response.failed",
                        "response": {
                            "error": {"code": "usage_not_included", "message": "nope"}
                        },
                    },
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )

    _mock_http(monkeypatch, llm_providers, handler)
    with pytest.raises(RuntimeError) as excinfo:
        await llm_providers._complete_chatgpt("prompt", "gpt-5.1-codex")
    assert "usage_not_included" in str(excinfo.value)


# --- admin endpoints ---------------------------------------------------------


async def test_chatgpt_login_endpoints_admin_flow(monkeypatch, client, make_user):
    _, admin_headers = await make_user("Admin")
    _, other_headers = await make_user("Someone")

    # Non-admin is rejected everywhere.
    for method, path in (
        ("GET", "/api/settings/chatgpt-login"),
        ("POST", "/api/settings/chatgpt-login"),
        ("POST", "/api/settings/chatgpt-login/poll"),
        ("DELETE", "/api/settings/chatgpt-login"),
    ):
        resp = await client.request(method, path, headers=other_headers)
        assert resp.status_code == 403, (method, path, resp.text)

    def handler(request):
        url = str(request.url)
        if url.endswith("/usercode"):
            return httpx.Response(
                200,
                json={"device_auth_id": "dev-1", "user_code": "WXYZ-9876"},
            )
        if url.endswith("/deviceauth/token"):
            return httpx.Response(
                200,
                json={"authorization_code": "c", "code_verifier": "v"},
            )
        return httpx.Response(
            200,
            json={"access_token": _access_token(), "refresh_token": "rt"},
        )

    _mock_http(monkeypatch, chatgpt_auth, handler)

    resp = await client.get("/api/settings/chatgpt-login", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["connected"] is False

    resp = await client.post("/api/settings/chatgpt-login", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["user_code"] == "WXYZ-9876"

    resp = await client.post("/api/settings/chatgpt-login/poll", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "connected"

    resp = await client.get("/api/settings/chatgpt-login", headers=admin_headers)
    assert resp.json()["connected"] is True

    resp = await client.delete("/api/settings/chatgpt-login", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["connected"] is False
    assert chatgpt_auth.has_auth() is False


async def test_chatgpt_login_start_maps_device_error_to_502(
    monkeypatch, client, make_user
):
    _, admin_headers = await make_user("Admin")

    def handler(request):
        return httpx.Response(403)

    _mock_http(monkeypatch, chatgpt_auth, handler)
    resp = await client.post("/api/settings/chatgpt-login", headers=admin_headers)
    assert resp.status_code == 502, resp.text
    assert "enable it in ChatGPT Settings" in resp.json()["detail"]
