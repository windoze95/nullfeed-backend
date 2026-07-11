"""Runtime, admin-controlled AI configuration for the discovery pipeline.

Lets an admin manage the discovery providers from the app instead of only via
container env vars: the anthropic/gemini/openai API keys and the embed/rank
provider+model selection. (The ChatGPT sign-in — the fourth rank provider —
is stored separately in ``chatgpt_auth``.)

Storage mirrors the established runtime-secret pattern (``chatgpt_auth`` tokens,
YouTube cookies): a single 0600 JSON file under ``settings.config_path``,
written atomically, read fresh on every call so every uvicorn/Celery worker on
the shared config volume sees an edit immediately.

Precedence is runtime-over-env: a value set here overrides the env/``settings``
default, and clearing it reverts to the env value — so existing env-based
deployments keep working untouched, and an operator can override live without a
redeploy. Keys are write-only: they are never returned by any read here (only
``key_status`` — configured/source/masked last 4) and never logged.
"""

import json
import os
from pathlib import Path

from app.config import settings

KEY_PROVIDERS = ("anthropic", "gemini", "openai")
SELECTION_ROLES = ("embed", "rank")

_ENV_KEY_ATTR = {
    "anthropic": "anthropic_api_key",
    "gemini": "gemini_api_key",
    "openai": "openai_api_key",
}
_FILENAME = "ai_config.json"


def _config_path() -> Path:
    return Path(settings.config_path) / _FILENAME


def _write_private(path: Path, data: dict) -> None:
    """Atomic 0600 write (tmp + rename); mirrors chatgpt_auth._write_private."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    tmp.write_text(json.dumps(data))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _load() -> dict:
    try:
        data = json.loads(_config_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _env_key(provider: str) -> str:
    attr = _ENV_KEY_ATTR.get(provider)
    return getattr(settings, attr, "") if attr else ""


def _runtime_key(provider: str) -> str:
    return _str((_load().get("keys") or {}).get(provider))


# --- reads (used by llm_providers / recommendation / ad_segments) -----------


def get_key(provider: str) -> str:
    """The effective key: runtime override if set, else the env value.

    Returns "" when neither is set, and always "" for "chatgpt" (which has no
    key — it authenticates via the ChatGPT sign-in).
    """
    return _runtime_key(provider) or _env_key(provider)


def get_embed_selection() -> tuple[str, str]:
    return _selection("embed")


def get_rank_selection() -> tuple[str, str]:
    return _selection("rank")


def _selection(role: str) -> tuple[str, str]:
    store = _load()
    provider = _str(store.get(f"{role}_provider"))
    model = _str(store.get(f"{role}_model"))
    # Return the runtime (provider, model) as a UNIT whenever either is set —
    # never pair a runtime provider with an env model (or vice versa), which
    # would mismatch vendor and model.
    if provider or model:
        return provider, model
    if role == "embed":
        return settings.discovery_embed_provider, settings.discovery_embed_model
    return settings.discovery_rank_provider, settings.discovery_rank_model


# --- writes (admin endpoints only) ------------------------------------------


def set_key(provider: str, key: str) -> None:
    store = _load()
    keys = dict(store.get("keys") or {})
    keys[provider] = key
    store["keys"] = keys
    _write_private(_config_path(), store)


def clear_key(provider: str) -> None:
    """Remove a runtime key, reverting that provider to its env value."""
    store = _load()
    keys = dict(store.get("keys") or {})
    if keys.pop(provider, None) is not None:
        store["keys"] = keys
        _write_private(_config_path(), store)


def set_selection(role: str, provider: str, model: str) -> None:
    store = _load()
    store[f"{role}_provider"] = provider
    store[f"{role}_model"] = model
    _write_private(_config_path(), store)


# --- status (write-only: never returns a raw key) ---------------------------


def key_status(provider: str) -> dict:
    runtime = _runtime_key(provider)
    env = _env_key(provider)
    effective = runtime or env
    return {
        "configured": bool(effective),
        "source": "runtime" if runtime else ("env" if env else None),
        # Masked hint; short/secret-like values are hidden entirely.
        "last4": effective[-4:] if len(effective) >= 8 else None,
    }


def selection_status(role: str) -> dict:
    provider, model = _selection(role)
    store = _load()
    runtime_set = bool(
        _str(store.get(f"{role}_provider")) or _str(store.get(f"{role}_model"))
    )
    return {
        "provider": provider,
        "model": model,
        "source": "runtime" if runtime_set else "env",
    }


def clear() -> None:
    """Test hook: drop the whole runtime store."""
    _config_path().unlink(missing_ok=True)
