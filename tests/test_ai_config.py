"""Runtime AI-config store: getters, env fallback, masking, and endpoints."""

import json

import pytest

import app.services.ai_config as ai_config
from app.config import settings

pytestmark = pytest.mark.asyncio


# --- getters + precedence ----------------------------------------------------


async def test_get_key_runtime_overrides_env(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "env-key")
    # No runtime store yet -> env value.
    assert ai_config.get_key("gemini") == "env-key"

    ai_config.set_key("gemini", "runtime-key")
    assert ai_config.get_key("gemini") == "runtime-key"

    # Clearing reverts to env.
    ai_config.clear_key("gemini")
    assert ai_config.get_key("gemini") == "env-key"


async def test_get_key_empty_when_neither_set(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    assert ai_config.get_key("openai") == ""
    # chatgpt never has a key here.
    assert ai_config.get_key("chatgpt") == ""


async def test_corrupt_store_falls_back_to_env(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "env-anthropic")
    ai_config._config_path().write_text("{ not valid json")
    assert ai_config.get_key("anthropic") == "env-anthropic"
    assert ai_config.get_embed_selection() == (
        settings.discovery_embed_provider,
        settings.discovery_embed_model,
    )


async def test_malformed_keys_field_degrades_and_is_repairable(monkeypatch):
    # Valid JSON, but 'keys' is hand-edited to a non-dict. Reads must degrade
    # to env, not crash, and a write must repair the store.
    monkeypatch.setattr(settings, "gemini_api_key", "env-gemini")
    for bad in ('{"keys": "sk-x"}', '{"keys": ["openai"]}', '{"keys": 5}'):
        ai_config._config_path().write_text(bad)
        assert ai_config.get_key("gemini") == "env-gemini"
        assert ai_config.key_status("gemini")["source"] == "env"
        # Writing repairs the malformed store rather than raising.
        ai_config.set_key("openai", "sk-repaired")
        assert ai_config.get_key("openai") == "sk-repaired"
        ai_config.clear()


async def test_selection_returned_as_a_unit(monkeypatch):
    # Env has a rank model but no provider; a runtime provider must NOT be
    # paired with the env model.
    monkeypatch.setattr(settings, "discovery_rank_provider", "")
    monkeypatch.setattr(settings, "discovery_rank_model", "env-model")

    # Nothing runtime -> env pair.
    assert ai_config.get_rank_selection() == ("", "env-model")

    # Runtime provider set (no runtime model) -> the runtime pair as a unit,
    # env model dropped.
    ai_config.set_selection("rank", "gemini", "")
    assert ai_config.get_rank_selection() == ("gemini", "")


async def test_key_status_masks_and_reports_source(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "")
    assert ai_config.key_status("gemini") == {
        "configured": False,
        "source": None,
        "last4": None,
    }

    ai_config.set_key("gemini", "abcdef1234")
    status = ai_config.key_status("gemini")
    assert status["configured"] is True
    assert status["source"] == "runtime"
    assert status["last4"] == "1234"

    # A short secret is fully masked.
    ai_config.set_key("openai", "short")
    assert ai_config.key_status("openai")["last4"] is None


async def test_store_file_is_0600(monkeypatch):
    import stat

    ai_config.set_key("openai", "sk-secret")
    mode = stat.S_IMODE(ai_config._config_path().stat().st_mode)
    assert mode == 0o600
    # The raw key lives in the file but is never returned by a getter/status.
    on_disk = json.loads(ai_config._config_path().read_text())
    assert on_disk["keys"]["openai"] == "sk-secret"
    assert "sk-secret" not in json.dumps(ai_config.key_status("openai"))


# --- resolver integration ----------------------------------------------------


async def test_runtime_key_enables_resolution(monkeypatch):
    import app.services.llm_providers as llm_providers

    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "discovery_embed_provider", "")
    monkeypatch.setattr(settings, "discovery_rank_provider", "")
    monkeypatch.setattr(settings, "discovery_embed_model", "")
    monkeypatch.setattr(settings, "discovery_rank_model", "")

    assert llm_providers.resolve_embed_provider() is None

    ai_config.set_key("gemini", "runtime-gemini")
    assert llm_providers.resolve_embed_provider() == ("gemini", "gemini-embedding-2")
    assert llm_providers.resolve_rank_provider() == ("gemini", "gemini-3.5-flash")

    # A runtime selection pins the rank provider.
    ai_config.set_key("openai", "runtime-openai")
    ai_config.set_selection("rank", "openai", "")
    assert llm_providers.resolve_rank_provider() == ("openai", "gpt-5.6-luna")


# --- admin endpoints ---------------------------------------------------------


async def test_ai_provider_endpoints_flow(monkeypatch, client, make_user):
    _, admin_headers = await make_user("Admin")
    _, other_headers = await make_user("Someone")
    monkeypatch.setattr(settings, "gemini_api_key", "")

    # Non-admin is refused.
    for method, path in (
        ("GET", "/api/settings/ai-providers"),
        ("PUT", "/api/settings/ai-providers/keys/gemini"),
        ("DELETE", "/api/settings/ai-providers/keys/gemini"),
        ("PUT", "/api/settings/ai-providers/selection/rank"),
    ):
        kwargs = {"headers": other_headers}
        if method == "PUT":
            kwargs["json"] = {"key": "x"} if "keys" in path else {"provider": ""}
        resp = await client.request(method, path, **kwargs)
        assert resp.status_code == 403, (method, path, resp.text)

    # Set a key; it's reported configured but never echoed.
    resp = await client.put(
        "/api/settings/ai-providers/keys/gemini",
        headers=admin_headers,
        json={"key": "sk-gemini-secret"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["keys"]["gemini"]["configured"] is True
    assert body["keys"]["gemini"]["source"] == "runtime"
    assert body["availability"]["gemini"] is True
    assert "sk-gemini-secret" not in resp.text

    # Pin the rank provider.
    resp = await client.put(
        "/api/settings/ai-providers/selection/rank",
        headers=admin_headers,
        json={"provider": "gemini", "model": ""},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["rank"]["effective"] == {
        "provider": "gemini",
        "model": "gemini-3.5-flash",
    }

    # Clear the key -> reverts to env (empty here).
    resp = await client.delete(
        "/api/settings/ai-providers/keys/gemini", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["keys"]["gemini"]["configured"] is False


async def test_ai_provider_endpoint_validation(monkeypatch, client, make_user):
    _, admin_headers = await make_user("Admin")

    # Unknown provider / role -> 404.
    resp = await client.put(
        "/api/settings/ai-providers/keys/cohere",
        headers=admin_headers,
        json={"key": "x"},
    )
    assert resp.status_code == 404, resp.text

    resp = await client.put(
        "/api/settings/ai-providers/selection/sideways",
        headers=admin_headers,
        json={"provider": "gemini"},
    )
    assert resp.status_code == 404, resp.text

    # chatgpt/anthropic are not valid EMBED providers -> 400.
    resp = await client.put(
        "/api/settings/ai-providers/selection/embed",
        headers=admin_headers,
        json={"provider": "chatgpt"},
    )
    assert resp.status_code == 400, resp.text

    # A model override without a provider -> 400.
    resp = await client.put(
        "/api/settings/ai-providers/selection/rank",
        headers=admin_headers,
        json={"provider": "", "model": "some-model"},
    )
    assert resp.status_code == 400, resp.text
