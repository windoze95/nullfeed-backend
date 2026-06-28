"""YouTube WebSub (PubSubHubbub) subscribe lifecycle + push parsing.

A pure accelerator on top of the RSS/adaptive poller: when ``websub_callback_url``
is configured, NullFeed asks YouTube's hub to PUSH new-upload notifications to its
public callback so discovery is near-real-time instead of waiting for the next
adaptive poll. Everything here no-ops when WebSub is disabled, and the poller
remains the unchanged always-on fallback (a missed/late push is still caught by
the next RSS poll).

This module owns the sync (Celery-side) pieces:

  * :func:`subscribe_channel` POSTs a ``hub.mode=subscribe`` form to the hub;
  * :func:`sync_subscriptions` is the beat task body that (re)subscribes every
    tracked UC channel whose lease is missing or near expiry;
  * :func:`parse_push` / :func:`channel_id_for_topic` parse the Atom push body
    and topic URL for the callback router.

The HTTP calls are plain synchronous ``httpx`` so the Celery beat can call them
directly, mirroring :mod:`app.services.push_gateway`.
"""

import logging
import xml.etree.ElementTree as ET
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.channel import Channel
from app.models.subscription import UserSubscription
from app.services.download_manager import YOUTUBE_RSS_FEED_URL
from app.utils.time import utcnow_naive
from app.utils.websub import websub_secret

logger = logging.getLogger(__name__)

# Atom + YouTube namespaces in a push body (same feed schema as the RSS poll).
# ElementTree matches tags by their {namespace}localname form.
_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_YT_NS = "{http://www.youtube.com/xml/schemas/2015}"

# Renew a subscription once it is within this fraction of its lease from expiry,
# so a renewal (and the hub's async re-verification) has comfortable headroom
# before the current lease lapses. With the default 5-day lease this renews in
# the final day.
_RENEW_BEFORE_FRACTION = 0.2

# httpx timeout (seconds) for a hub request.
_TIMEOUT = 15.0


def websub_enabled() -> bool:
    """True when a public callback URL is configured; everything no-ops if not."""
    return bool(settings.websub_callback_url)


def topic_url(youtube_channel_id: str) -> str:
    """The WebSub topic (the channel's Atom feed URL) for a canonical UC id."""
    return f"{YOUTUBE_RSS_FEED_URL}?channel_id={youtube_channel_id}"


def channel_id_for_topic(topic: str | None) -> str | None:
    """Extract the ``channel_id`` query param from a topic URL (or None)."""
    if not topic:
        return None
    try:
        values = parse_qs(urlparse(topic).query).get("channel_id")
    except ValueError:
        return None
    return values[0] if values else None


def parse_push(raw_body: bytes) -> dict:
    """Parse a WebSub Atom push into ``{"channel_id", "video_ids"}``.

    Returns the feed's canonical UC ``channel_id`` (from the first entry's
    ``yt:channelId``, falling back to a feed-level one) and the newest-first list
    of ``yt:videoId``s. Deletion tombstones carry no ``yt:videoId`` and are
    ignored — we only catalog new uploads. A body that doesn't parse yields empty
    values so the caller treats the push as a no-op rather than erroring.

    The body is YouTube-originated and HMAC-verified by the caller before this
    runs, so stdlib ElementTree is used directly (no untrusted-XML concerns).
    """
    try:
        root = ET.fromstring(raw_body)
    except ET.ParseError:
        return {"channel_id": None, "video_ids": []}

    channel_id: str | None = None
    feed_channel_el = root.find(f"{_YT_NS}channelId")
    if feed_channel_el is not None and feed_channel_el.text:
        channel_id = feed_channel_el.text.strip() or None

    video_ids: list[str] = []
    for entry in root.findall(f"{_ATOM_NS}entry"):
        vid_el = entry.find(f"{_YT_NS}videoId")
        video_id = (vid_el.text or "").strip() if vid_el is not None else ""
        if video_id:
            video_ids.append(video_id)
        if channel_id is None:
            chan_el = entry.find(f"{_YT_NS}channelId")
            if chan_el is not None and chan_el.text:
                channel_id = chan_el.text.strip() or None

    return {"channel_id": channel_id, "video_ids": video_ids}


def subscribe_channel(channel: Channel, *, mode: str = "subscribe") -> bool:
    """POST a subscribe/unsubscribe request for one channel to the hub.

    Sends the form the hub expects (``hub.callback``/``hub.topic``/``hub.mode``/
    ``hub.verify=async``/``hub.secret``/``hub.lease_seconds``). With async verify
    the hub replies 202 and then calls our GET callback to confirm. Best-effort:
    returns ``False`` (logged, never raised) when WebSub is disabled or the hub
    request fails, so a single channel's failure can't abort the beat batch.
    """
    if not websub_enabled():
        return False
    payload = {
        "hub.callback": settings.websub_callback_url,
        "hub.topic": topic_url(channel.youtube_channel_id),
        "hub.mode": mode,
        "hub.verify": "async",
        "hub.secret": websub_secret(),
        "hub.lease_seconds": str(settings.websub_lease_seconds),
    }
    try:
        resp = httpx.post(settings.websub_hub_url, data=payload, timeout=_TIMEOUT)
    except httpx.HTTPError:
        logger.warning(
            "WebSub %s request failed for channel %s", mode, channel.id, exc_info=True
        )
        return False
    if resp.status_code // 100 != 2:
        logger.warning(
            "WebSub %s for channel %s returned HTTP %s",
            mode,
            channel.id,
            resp.status_code,
        )
        return False
    return True


def sync_subscriptions(db: Session) -> dict:
    """(Re)subscribe every tracked UC channel whose lease is missing/near expiry.

    The beat task body. No-ops entirely when WebSub is disabled. Otherwise it
    selects channels that (a) have at least one subscriber, (b) carry a canonical
    UC id (the only ids the Atom feed/topic is addressable by), and (c) have no
    recorded lease or one expiring within the renewal window, then subscribes
    each. On a successful POST the channel's ``websub_expires_at`` is stamped
    optimistically to ``now + lease``; on failure it is left as-is so the channel
    stays due and is retried on the next beat. Never touches the adaptive poll
    schedule, so the RSS fallback is unaffected.
    """
    if not websub_enabled():
        return {"status": "disabled", "subscribed": 0}

    now = utcnow_naive()
    renew_margin = timedelta(
        seconds=int(settings.websub_lease_seconds * _RENEW_BEFORE_FRACTION)
    )
    due_cutoff = now + renew_margin

    channels = (
        db.execute(
            select(Channel)
            .join(UserSubscription, UserSubscription.channel_id == Channel.id)
            .where(Channel.youtube_channel_id.like("UC%"))
            .where(
                (Channel.websub_expires_at.is_(None))
                | (Channel.websub_expires_at <= due_cutoff)
            )
            .distinct()
        )
        .scalars()
        .all()
    )

    subscribed = 0
    lease = timedelta(seconds=settings.websub_lease_seconds)
    for channel in channels:
        if subscribe_channel(channel, mode="subscribe"):
            # Optimistic: the hub confirms asynchronously via our GET callback.
            # The next beat renews before this lapses; a failed/declined verify
            # just means the next RSS poll still catches the upload.
            channel.websub_expires_at = utcnow_naive() + lease
            subscribed += 1
    db.commit()

    logger.info(
        "WebSub sync: %d channel(s) due, %d (re)subscribed", len(channels), subscribed
    )
    return {"status": "ok", "due": len(channels), "subscribed": subscribed}
