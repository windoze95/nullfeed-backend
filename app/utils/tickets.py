"""Short-lived, HMAC-signed access tickets for media streams and the WebSocket (#30).

The long-lived session token used to ride media URLs (``/stream``,
``/preview-stream``) and the WebSocket handshake as a ``?token=`` query param.
Query strings leak into proxy/access logs, browser history, and shared links, so
a single captured log line was a full session hijack.

A ticket is a stateless capability minted from a session-authenticated request,
scoped narrowly (one video for playback, the user for the socket) and valid for
only a few minutes, so a leaked URL is worth almost nothing. Wire format::

    base64url(payload).base64url(hmac_sha256(payload))

where ``payload`` is compact JSON ``{"scope","user_id","video_id"?,"exp"}``.
Verification is constant-time and rejects anything tampered, expired, or scoped
to a different user/video.

Signing secret: a single stable secret shared by every worker. It is read from
``settings.stream_ticket_secret`` (env ``STREAM_TICKET_SECRET``) when set;
otherwise the app generates one once and persists it under ``config_path`` so it
survives restarts and is shared across workers on the same volume. A
per-process random value is deliberately avoided — worker A's ticket would then
fail verification on worker B.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from app.config import settings

# Ticket scopes. A ticket only authorizes the endpoint family it was minted for;
# replaying a ws-ticket on /stream (or vice versa) is rejected by the scope check.
SCOPE_STREAM = "stream"
SCOPE_WS = "ws"

# Tickets are deliberately short-lived: long enough to start playback / open the
# socket, short enough that a leaked URL expires before it is useful.
TICKET_TTL_SECONDS = 300  # 5 minutes

# Filename of the auto-generated secret persisted under settings.config_path.
_SECRET_FILENAME = "stream_ticket_secret"

# Cache for the persisted secret so we read/create the file at most once per
# process. An explicitly configured secret is never cached here (it already
# lives in settings), so tests can override it freely.
_persisted_secret: str | None = None


class TicketError(Exception):
    """Raised when a ticket is malformed, tampered, expired, or mis-scoped."""


def _b64encode(raw: bytes) -> str:
    """URL-safe base64 without padding (so tickets stay clean in query strings)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _load_or_create_persisted_secret() -> str:
    """Read the persisted signing secret, generating it once if it is absent.

    Concurrency-safe across workers booting together: the secret is written to a
    private temp file and then hard-linked into place, which is atomic and fails
    if the target already exists. Whoever wins the link returns its own secret;
    everyone else reads the winner's. The target is therefore never observed
    half-written, and all workers converge on a single value.
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


def _signing_secret() -> bytes:
    configured = settings.stream_ticket_secret
    if configured:
        return configured.encode("utf-8")
    global _persisted_secret
    if _persisted_secret is None:
        _persisted_secret = _load_or_create_persisted_secret()
    return _persisted_secret.encode("utf-8")


def _sign(payload_segment: str) -> str:
    digest = hmac.new(
        _signing_secret(), payload_segment.encode("ascii"), hashlib.sha256
    ).digest()
    return _b64encode(digest)


def mint_ticket(
    scope: str,
    user_id: str,
    *,
    video_id: str | None = None,
    ttl_seconds: int = TICKET_TTL_SECONDS,
) -> tuple[str, int]:
    """Mint a signed ticket. Returns ``(ticket, expires_in_seconds)``."""
    payload: dict[str, object] = {
        "scope": scope,
        "user_id": user_id,
        "exp": int(time.time()) + ttl_seconds,
    }
    if video_id is not None:
        payload["video_id"] = video_id
    segment = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    ticket = f"{segment}.{_sign(segment)}"
    return ticket, ttl_seconds


def verify_ticket(
    ticket: str,
    *,
    scope: str,
    user_id: str | None = None,
    video_id: str | None = None,
) -> str:
    """Verify a ticket and return the ``user_id`` it was minted for.

    Raises :class:`TicketError` if the ticket is malformed, tampered, expired, of
    the wrong ``scope``, or (when the corresponding argument is given) bound to a
    different ``user_id`` / ``video_id``. The signature is checked first, in
    constant time, before any field of the payload is trusted.
    """
    if not ticket or ticket.count(".") != 1:
        raise TicketError("malformed ticket")

    segment, signature = ticket.split(".", 1)
    if not hmac.compare_digest(signature, _sign(segment)):
        raise TicketError("bad signature")

    try:
        payload = json.loads(_b64decode(segment))
    except (ValueError, TypeError) as exc:
        raise TicketError("undecodable payload") from exc
    if not isinstance(payload, dict):
        raise TicketError("bad payload")

    if payload.get("scope") != scope:
        raise TicketError("wrong scope")

    exp = payload.get("exp")
    if not isinstance(exp, int) or isinstance(exp, bool) or time.time() >= exp:
        raise TicketError("expired")

    token_user_id = payload.get("user_id")
    if not isinstance(token_user_id, str) or not token_user_id:
        raise TicketError("missing user")
    if user_id is not None and token_user_id != user_id:
        raise TicketError("wrong user")

    if video_id is not None and payload.get("video_id") != video_id:
        raise TicketError("wrong video")

    return token_user_id
