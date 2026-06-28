"""Public WebSub (PubSubHubbub) callback for near-real-time new uploads.

YouTube's hub calls these endpoints directly, so — unlike every other NullFeed
route — they are UNAUTHENTICATED. Trust comes instead from the protocol:

  * ``GET /api/websub/callback`` is the hub's subscription-verification probe. We
    echo ``hub.challenge`` (text/plain 200) only for a ``hub.topic`` that maps to
    a channel we actually track; anything else 404s, so we can't be coerced into
    confirming subscriptions we never requested.
  * ``POST /api/websub/callback`` is a content-distribution push. Its body is
    authenticated by recomputing the ``X-Hub-Signature`` HMAC with our shared
    secret; an absent/forged signature 404s WITHOUT revealing why. A verified
    push is parsed for ``yt:videoId``s and the genuinely-new ones are cataloged
    off the request path via the same poller code (Celery), so the response stays
    fast and duplicate pushes are idempotent.

The whole router is gated on ``websub_enabled()``: when no callback URL is
configured the feature is off and both endpoints 404, leaving the RSS/adaptive
poller as the sole (unchanged) discovery path.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.channel import Channel
from app.models.subscription import UserSubscription
from app.services.websub import channel_id_for_topic, parse_push, websub_enabled
from app.tasks.download_tasks import ingest_websub_push_task
from app.utils.websub import verify_signature

router = APIRouter(prefix="/api/websub", tags=["websub"])
logger = logging.getLogger(__name__)


async def _tracked_channel_id(uc_id: str, db: AsyncSession) -> str | None:
    """Return our internal id for a tracked (subscribed) UC channel, else None.

    "Tracked" means a Channel row with this canonical UC id that has at least one
    subscriber — exactly the set the subscribe beat task subscribes — so the
    callback only ever acts on subscriptions we requested.
    """
    result = await db.execute(
        select(Channel.id)
        .join(UserSubscription, UserSubscription.channel_id == Channel.id)
        .where(Channel.youtube_channel_id == uc_id)
        .limit(1)
    )
    return result.scalar_one_or_none()


@router.get("/callback")
async def verify_subscription(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Hub verification probe: echo ``hub.challenge`` for a tracked topic.

    Returns the challenge as text/plain 200 when ``hub.mode`` is subscribe or
    unsubscribe and ``hub.topic`` resolves to a channel we track; 404 otherwise
    (including when WebSub is disabled), which tells the hub the (un)subscription
    was not confirmed.
    """
    if not websub_enabled():
        raise HTTPException(status_code=404, detail="WebSub not enabled")

    params = request.query_params
    mode = params.get("hub.mode")
    challenge = params.get("hub.challenge")
    uc_id = channel_id_for_topic(params.get("hub.topic"))

    if (
        mode in ("subscribe", "unsubscribe")
        and challenge
        and uc_id
        and await _tracked_channel_id(uc_id, db) is not None
    ):
        return PlainTextResponse(challenge)

    raise HTTPException(status_code=404, detail="Unknown subscription")


@router.post("/callback")
async def receive_push(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Authenticated content push: catalog genuinely-new uploads off the request path.

    Verifies ``X-Hub-Signature`` against the raw body, then parses the Atom push
    for video ids and dispatches a Celery job to catalog the new ones (yt-dlp can
    be slow, so it must not block this response). Always returns quickly: 204 once
    accepted; 404 for a bad/absent signature (without revealing why) or when the
    feature is disabled.
    """
    if not websub_enabled():
        raise HTTPException(status_code=404, detail="WebSub not enabled")

    raw = await request.body()
    if not verify_signature(raw, request.headers.get("X-Hub-Signature")):
        # Do not reveal whether the topic is tracked or why verification failed;
        # an unauthenticated body must never reach the catalog path.
        raise HTTPException(status_code=404, detail="Invalid signature")

    parsed = parse_push(raw)
    uc_id = parsed["channel_id"]
    video_ids = parsed["video_ids"]
    if not uc_id or not video_ids:
        # Nothing actionable (e.g. a deletion tombstone or an empty body).
        return Response(status_code=204)

    channel_id = await _tracked_channel_id(uc_id, db)
    if channel_id is None:
        # Signed, but for a channel we no longer track; accept and ignore.
        return Response(status_code=204)

    # Hand off to Celery: idempotent cataloging of only the genuinely-new ids
    # happens there, so this handler stays fast for the hub.
    ingest_websub_push_task.delay(channel_id, video_ids)
    return Response(status_code=204)
