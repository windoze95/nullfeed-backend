"""Durable HMAC secret + push-signature verification for the WebSub subscriber.

WebSub (PubSubHubbub) lets YouTube's hub PUSH new-upload notifications to a
public callback instead of NullFeed polling each channel's Atom feed. Two places
share one secret:

  * a subscribe request sends it as ``hub.secret`` so the hub will sign pushes;
  * every push carries an ``X-Hub-Signature`` HMAC of the raw body keyed by that
    same secret, which we recompute to authenticate the push before acting on it.

The secret MUST be identical across every worker and survive restarts: a push
delivered to (and verified by) one worker may have been subscribed by another, so
a per-process random value would make pushes fail verification on the worker that
didn't mint the subscription. Exactly like :mod:`app.utils.tickets`, it is read
from a file persisted under ``settings.config_path``, generated once via an
atomic create-only hard-link so racing workers converge on a single value.
"""

import hashlib
import hmac
import os
import secrets
from pathlib import Path

from app.config import settings

# Filename of the persisted secret under settings.config_path.
_SECRET_FILENAME = "websub_secret"

# X-Hub-Signature digests we accept, keyed by the algorithm prefix the hub uses
# (e.g. ``sha1=<hex>`` / ``sha256=<hex>``). Google's pubsubhubbub sends sha1.
_SUPPORTED_ALGOS = {"sha1": hashlib.sha1, "sha256": hashlib.sha256}

# Cache so the file is read/created at most once per process; the file remains
# authoritative across workers.
_persisted_secret: str | None = None


def _load_or_create_persisted_secret() -> str:
    """Read the persisted secret, generating it once if absent.

    Concurrency-safe across workers booting together: a private temp file is
    written then hard-linked into place, which is atomic and fails if the target
    already exists. Whoever wins the link returns its own secret; everyone else
    reads the winner's, so all workers converge on a single value and the target
    is never observed half-written. Mirrors
    :func:`app.utils.tickets._load_or_create_persisted_secret`.
    """
    path = Path(settings.config_path) / _SECRET_FILENAME
    try:
        existing = path.read_text().strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass

    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = secrets.token_urlsafe(48)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(candidate)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    try:
        os.link(tmp, path)  # atomic create-only claim; raises if path exists
        return candidate
    except FileExistsError:
        return path.read_text().strip()
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def websub_secret() -> str:
    """Return the shared WebSub secret, creating + persisting it on first use."""
    global _persisted_secret
    if _persisted_secret is None:
        _persisted_secret = _load_or_create_persisted_secret()
    return _persisted_secret


def verify_signature(raw_body: bytes, header_value: str | None) -> bool:
    """Constant-time check of an ``X-Hub-Signature`` against the raw push body.

    The header is ``<algo>=<hexdigest>`` (e.g. ``sha1=...``). Returns ``False``
    for a missing/malformed header or an unsupported algorithm — so an unsigned
    or tampered push is rejected — and verifies the HMAC in constant time
    otherwise. The body must be the EXACT bytes the hub sent (re-encoding it
    would change the digest), so callers pass ``await request.body()`` verbatim.
    """
    if not header_value or "=" not in header_value:
        return False
    algo_name, _, sent_hex = header_value.partition("=")
    algo = _SUPPORTED_ALGOS.get(algo_name.strip().lower())
    if algo is None or not sent_hex.strip():
        return False
    expected = hmac.new(websub_secret().encode("utf-8"), raw_body, algo).hexdigest()
    return hmac.compare_digest(expected, sent_hex.strip())


def _reset_cache() -> None:
    """Test hook: drop the in-process secret cache (the file stays authoritative)."""
    global _persisted_secret
    _persisted_secret = None
