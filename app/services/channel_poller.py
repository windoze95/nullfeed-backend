import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.channel import Channel
from app.models.subscription import UserSubscription
from app.models.user_video_ref import REF_KIND_CACHE, UserVideoRef
from app.models.video import Video
from app.services.download_manager import (
    fetch_channel_images,
    fetch_channel_metadata,
    fetch_channel_rss,
    fetch_channel_videos,
    fetch_videos_metadata,
)
from app.services.progress_broadcaster import publish_new_episode
from app.services.push_gateway import send_to_users
from app.utils.time import utcnow_naive
from app.utils.unplayable import SOFT_REASONS

logger = logging.getLogger(__name__)


def _as_naive_utc(value: datetime | None) -> datetime | None:
    """Normalize a datetime to naive UTC for safe comparisons."""
    if value is not None and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _reschedule_channel(channel: Channel, *, found_new: bool) -> None:
    """Recompute a channel's next poll time from this poll's outcome.

    Multiplicative backoff bounded by a configurable floor and cap: a poll that
    found new uploads divides the interval by the backoff factor (toward the
    floor, since the channel looks active); an empty poll multiplies it (toward
    the cap, since the channel looks dormant). With the beat polling only DUE
    channels, the effective cadence converges on each channel's observed upload
    frequency. Mutates the channel in place; the caller commits.
    """
    from app.config import settings

    floor = settings.poll_interval_floor_minutes
    # Coerce the bounds so a misconfiguration can never invert them or divide by
    # zero, which would otherwise produce a nonsensical schedule.
    cap = max(floor, settings.poll_interval_cap_minutes)
    factor = settings.poll_interval_backoff_factor or 1.0

    current = channel.poll_interval_minutes or floor
    interval = current / factor if found_new else current * factor
    interval = max(floor, min(cap, int(round(interval))))

    channel.poll_interval_minutes = interval
    channel.next_poll_at = utcnow_naive() + timedelta(minutes=interval)


def _backoff_failed_channel(channel_id: str, db: Session) -> None:
    """Push a failed channel's next poll out so it doesn't retry every beat.

    Called after a poll raised and the session was rolled back, so it reloads
    the channel in a fresh transaction. Best-effort: a failure here must not
    abort the rest of the batch, so it only logs.
    """
    try:
        channel = db.get(Channel, channel_id)
        if channel is not None:
            _reschedule_channel(channel, found_new=False)
            db.commit()
    except Exception:
        logger.exception("Could not back off failed channel %s", channel_id)
        db.rollback()


def list_channel_ids_to_poll(db: Session, *, due_only: bool = False) -> list[str]:
    """Return the distinct subscribed channel ids that should be polled.

    When ``due_only`` is set (the periodic beat) only channels whose adaptive
    schedule has come due (``next_poll_at <= now``) are returned — a cheap,
    indexed query the beat runs on every wake to decide how much work to fan
    out. When unset (an explicit "refresh everything" request, e.g.
    pull-to-refresh) every subscribed channel is returned regardless of its
    schedule. This is the work list the beat dispatches one per-channel job per.
    """
    stmt = (
        select(Channel.id)
        .join(UserSubscription, UserSubscription.channel_id == Channel.id)
        .distinct()
    )
    if due_only:
        stmt = stmt.where(Channel.next_poll_at <= utcnow_naive())

    return [row[0] for row in db.execute(stmt).all()]


def poll_all_channels(db: Session, *, due_only: bool = False) -> list[str]:
    """Poll subscribed channels in-process and return auto-download video IDs.

    Synchronous reference path: every due (or every subscribed, when
    ``due_only`` is unset) channel is polled sequentially on the one ``db``
    session. Production fans the same work out into isolated per-channel Celery
    jobs (see ``app.tasks.download_tasks.poll_all_channels_task``); this helper
    is retained for direct/inline use and is exercised by the adaptive tests.
    """
    channel_ids = list_channel_ids_to_poll(db, due_only=due_only)
    logger.info("Polling %d channels (due_only=%s)", len(channel_ids), due_only)

    all_auto_download_ids: list[str] = []
    for channel_id in channel_ids:
        try:
            poll_result = poll_single_channel(channel_id, db)
            all_auto_download_ids.extend(poll_result["auto_download_ids"])
        except Exception:
            logger.exception("Error polling channel %s", channel_id)
            db.rollback()
            # The frequent beat would otherwise retry a persistently failing
            # channel every wake; widen its cadence so it backs off like an
            # empty poll instead of hot-looping.
            _backoff_failed_channel(channel_id, db)

    return all_auto_download_ids


def poll_single_channel(channel_id: str, db: Session) -> dict:
    """
    Poll a single channel for new videos.
    Returns dict with cataloged_ids and auto_download_ids.
    """
    channel = db.get(Channel, channel_id)
    if not channel:
        logger.warning("Channel %s not found", channel_id)
        return {"cataloged_ids": [], "auto_download_ids": []}

    # The very first poll catalogs the back catalog; videos discovered then
    # must never be auto-downloaded for FUTURE_ONLY subscribers.
    had_initial_poll = channel.last_checked_at is not None

    if not had_initial_poll:
        # First-ever poll: ingest the back catalog via a full yt-dlp listing.
        # The RSS feed only exposes the ~15 newest uploads, so using it here
        # would permanently skip older videos.
        yt_videos = fetch_channel_videos(channel.youtube_channel_id)["videos"]
    else:
        # Routine poll: cheap RSS conditional GET. Only genuinely-new video IDs
        # fall through to yt-dlp for full metadata.
        yt_videos = _discover_routine(channel, db)
        if yt_videos is None:
            # 304 Not Modified, or the feed surfaced no unseen videos: nothing
            # to catalog. Record the poll and, since it was empty, widen the
            # cadence toward the cap. _discover_routine refreshed any validators
            # on the session; this commit persists them alongside next_poll_at.
            channel.last_checked_at = utcnow_naive()
            _reschedule_channel(channel, found_new=False)
            db.commit()
            return {"cataloged_ids": [], "auto_download_ids": []}

    # The routine poll updates last_checked_at and the adaptive cadence as part
    # of the catalog commit; the initial poll only catalogs (no prior schedule).
    return _catalog_videos(
        channel,
        yt_videos,
        db,
        had_initial_poll=had_initial_poll,
        update_schedule=True,
    )


def _catalog_videos(
    channel: Channel,
    yt_videos: list[dict],
    db: Session,
    *,
    had_initial_poll: bool,
    update_schedule: bool,
) -> dict:
    """Insert genuinely-new videos from a metadata batch and fan out the rest.

    Shared by the routine poll and the WebSub push handler. ``yt_videos`` is a
    list of yt-dlp metadata dicts (newest-first). New videos are inserted
    CATALOGED, subscriber refs are ensured, FUTURE_ONLY auto-download candidates
    flip to PENDING, and — only when ``had_initial_poll`` (so back-catalog
    ingestion stays silent) — a new_episode event + push is emitted per
    subscriber/new video.

    When ``update_schedule`` is set (the poller) ``last_checked_at`` and the
    adaptive cadence are folded into the same commit; the WebSub path passes
    ``False`` so the poll schedule (the RSS fallback) is left entirely untouched.
    Returns ``{"cataloged_ids", "auto_download_ids"}``.

    Entries carrying a hard ``unplayable_reason`` (members-only, premium,
    removed, …) are still cataloged — visibility is the point — but sit out
    auto-download and new-episode notifications, since fetching can't succeed
    and notifying about unwatchable content is noise.
    """
    channel_id = channel.id
    cataloged_ids: list[str] = []
    new_video_ids: list[str] = []
    new_videos_for_events: list[dict] = []
    # Every video this batch touched (new rows plus already-cataloged ones seen
    # in the feed); a single batched ref-ensure runs over them after the loop.
    video_ids_for_refs: list[str] = []

    # Batch the existence check: one query maps every feed id we already have to
    # its row id, replacing the per-video SELECT. id_by_ytid also absorbs rows
    # created during this loop so a duplicate feed entry resolves as existing
    # instead of being inserted twice.
    feed_ids = [v["youtube_video_id"] for v in yt_videos if v.get("youtube_video_id")]
    id_by_ytid: dict[str, str] = {}
    if feed_ids:
        id_by_ytid = {
            ytid: row_id
            for row_id, ytid in db.execute(
                select(Video.id, Video.youtube_video_id).where(
                    Video.youtube_video_id.in_(feed_ids)
                )
            ).all()
        }

    # yt-dlp returns the channel feed newest-first. Cataloged videos have no
    # upload date until downloaded, and listings fall back to created_at — so
    # stamp each new row with a synthetic timestamp that decreases down the
    # feed, preserving the feed order within this batch.
    poll_started_at = utcnow_naive()

    for index, yt_vid in enumerate(yt_videos):
        yt_video_id = yt_vid["youtube_video_id"]
        if not yt_video_id:
            continue

        existing_id = id_by_ytid.get(yt_video_id)
        if existing_id is not None:
            # Already cataloged (or created earlier in this same feed batch);
            # just ensure refs exist for it in the batched pass below.
            video_ids_for_refs.append(existing_id)
            continue

        # Parse upload_date into uploaded_at (stored as naive UTC)
        uploaded_at = None
        if yt_vid.get("upload_date"):
            try:
                uploaded_at = datetime.strptime(yt_vid["upload_date"], "%Y%m%d")
            except (ValueError, TypeError):
                pass

        # A video-inherent refusal known at discovery time (members-only /
        # premium badge, unaired premiere, or a classified extraction failure).
        # Hard reasons never auto-download (it can't succeed) and stay out of
        # new-episode pushes (notifying about unwatchable content is noise) —
        # the labeled row in the feed is the visibility.
        reason = yt_vid.get("unplayable_reason")
        hard_blocked = bool(reason) and reason not in SOFT_REASONS

        # Create new video record as CATALOGED (not PENDING)
        video = Video(
            id=str(uuid.uuid4()),
            youtube_video_id=yt_video_id,
            channel_id=channel_id,
            title=yt_vid.get("title", yt_video_id),
            duration_seconds=yt_vid.get("duration_seconds", 0),
            uploaded_at=uploaded_at,
            status="CATALOGED",
            unplayable_reason=reason,
            content_type=yt_vid.get("content_type"),
            created_at=poll_started_at - timedelta(seconds=index),
        )
        db.add(video)
        db.flush()
        # Record so a duplicate id later in this same feed resolves as existing.
        id_by_ytid[yt_video_id] = video.id

        video_ids_for_refs.append(video.id)
        if not hard_blocked:
            new_video_ids.append(video.id)
        cataloged_ids.append(video.id)
        if not hard_blocked:
            new_videos_for_events.append(
                {
                    "id": video.id,
                    "title": video.title,
                    "youtube_video_id": video.youtube_video_id,
                }
            )
        logger.info("New video cataloged: %s (%s)", yt_video_id, video.title)

    # One batched ref-ensure over every video this batch touched, instead of the
    # per-video, per-subscriber N+1 the old per-video helper issued.
    _ensure_user_refs_bulk(video_ids_for_refs, channel_id, db)

    # Determine auto-download candidates based on subscriber tracking modes
    auto_download_ids: list[str] = []
    if new_video_ids:
        auto_download_ids = _determine_auto_downloads(
            new_video_ids, channel_id, db, had_initial_poll
        )

    if update_schedule:
        channel.last_checked_at = utcnow_naive()
        # New rows -> the channel looks active, shorten toward the floor; an
        # empty poll -> looks dormant, lengthen toward the cap.
        _reschedule_channel(channel, found_new=bool(cataloged_ids))
    db.commit()

    # Notify subscribers about genuinely new episodes — never the back catalog
    # ingested on the very first poll (had_initial_poll is False then).
    if had_initial_poll and new_videos_for_events:
        _emit_new_episode_events(channel_id, new_videos_for_events, db)

    return {"cataloged_ids": cataloged_ids, "auto_download_ids": auto_download_ids}


def ingest_pushed_videos(
    channel_id: str, youtube_video_ids: list[str], db: Session
) -> dict:
    """Catalog WebSub-pushed video IDs for a channel via the normal poll path.

    The near-real-time counterpart to :func:`poll_single_channel`: instead of
    polling the Atom feed, YouTube's hub pushed the new video ids to our
    callback. We catalog the genuinely-new ones — skipping any already known, so
    the duplicate pushes the hub commonly sends are idempotent — exactly as the
    RSS discovery path does via :func:`_catalog_videos`, emitting new_episode
    events + pushes. The adaptive poll schedule is deliberately left untouched so
    the RSS poller remains the unchanged fallback.

    Returns ``{"cataloged_ids", "auto_download_ids"}`` like the poller.
    """
    empty: dict = {"cataloged_ids": [], "auto_download_ids": []}

    channel = db.get(Channel, channel_id)
    if channel is None:
        logger.warning("WebSub ingest: channel %s not found", channel_id)
        return empty

    # A channel that has never been polled has no back-catalog baseline; defer to
    # the poller for the initial ingest rather than treating a push as a flood of
    # "new" episodes (and spamming notifications for the back catalog).
    if channel.last_checked_at is None:
        logger.info(
            "WebSub ingest: channel %s has no initial poll yet; deferring to poller",
            channel_id,
        )
        return empty

    # Idempotency: keep only ids we have never cataloged, deduped and order-
    # preserving. A duplicate push thus resolves to an empty set and does no
    # yt-dlp work. _catalog_videos re-checks under the same transaction too, so
    # this is also safe under concurrent pushes.
    candidate_ids = [vid for vid in dict.fromkeys(youtube_video_ids) if vid]
    if not candidate_ids:
        return empty
    known = set(
        db.execute(
            select(Video.youtube_video_id).where(
                Video.youtube_video_id.in_(candidate_ids)
            )
        )
        .scalars()
        .all()
    )
    new_ids = [vid for vid in candidate_ids if vid not in known]
    if not new_ids:
        return empty

    logger.info("WebSub push: %d new video(s) for channel %s", len(new_ids), channel_id)
    yt_videos = fetch_videos_metadata(new_ids)
    if not yt_videos:
        return empty

    return _catalog_videos(
        channel, yt_videos, db, had_initial_poll=True, update_schedule=False
    )


def _discover_routine(channel: Channel, db: Session) -> list[dict] | None:
    """Routine (non-initial) discovery for a channel via its Atom upload feed.

    Returns:
      * a list of yt-dlp metadata dicts for genuinely-new video IDs (newest
        first) for the caller to catalog;
      * ``None`` when there is nothing to catalog — a 304 Not Modified, or a
        fresh feed with no unseen video IDs. On the ``ok`` path the refreshed
        HTTP validators are written to the session so the caller's commit
        persists them; the caller records ``last_checked_at``, reschedules the
        channel, and commits.

    Falls back to a full yt-dlp listing when RSS is unavailable for the channel
    (handle/username id, network error, or unparseable feed), preserving the
    original discovery behavior. That list is returned for the caller to catalog
    exactly as the initial poll does.
    """
    rss = fetch_channel_rss(
        channel.youtube_channel_id,
        etag=channel.rss_etag,
        last_modified=channel.rss_last_modified,
    )

    if rss["status"] == "unavailable":
        return fetch_channel_videos(channel.youtube_channel_id)["videos"]

    if rss["status"] == "not_modified":
        # Feed unchanged since the last poll: no new uploads, no work to do.
        return None

    # status == "ok": refresh the stored validators so the next poll can 304.
    # These persist as part of the caller's single commit.
    channel.rss_etag = rss["etag"]
    channel.rss_last_modified = rss["last_modified"]

    feed_ids = [e["youtube_video_id"] for e in rss["entries"] if e["youtube_video_id"]]
    new_ids: list[str] = []
    if feed_ids:
        existing = set(
            db.execute(
                select(Video.youtube_video_id).where(
                    Video.youtube_video_id.in_(feed_ids)
                )
            )
            .scalars()
            .all()
        )
        # feed_ids is newest-first; the comprehension preserves that order.
        new_ids = [vid for vid in feed_ids if vid not in existing]

    if not new_ids:
        return None

    logger.info("RSS surfaced %d new video(s) for channel %s", len(new_ids), channel.id)
    # The feed already named every id; pass the titles along so an id whose
    # extraction fails for a video-inherent reason (members-only, premiere) is
    # cataloged under its real title rather than the bare video id.
    rss_titles = {
        e["youtube_video_id"]: e["title"] for e in rss["entries"] if e.get("title")
    }
    return fetch_videos_metadata(new_ids, titles=rss_titles)


def refresh_stale_channel_metadata(db: Session) -> int:
    """Refresh metadata for channels with missing or stale images.

    Returns the number of channels updated.
    """
    from app.config import settings

    staleness_threshold = utcnow_naive() - timedelta(
        hours=settings.metadata_refresh_interval_hours
    )

    # Channels that have at least one subscriber and need a metadata refresh:
    # either never refreshed, or refreshed before the staleness threshold,
    # or missing images.
    result = db.execute(
        select(Channel)
        .join(UserSubscription, UserSubscription.channel_id == Channel.id)
        .where(
            (Channel.metadata_refreshed_at.is_(None))
            | (Channel.metadata_refreshed_at <= staleness_threshold)
            | (Channel.avatar_url.is_(None))
            | (Channel.banner_url.is_(None))
        )
        .distinct()
    )
    channels = result.scalars().all()
    logger.info("Refreshing metadata for %d channels", len(channels))

    updated = 0
    for channel in channels:
        try:
            _refresh_single_channel_metadata(channel, db)
            updated += 1
        except Exception:
            logger.exception("Error refreshing metadata for channel %s", channel.id)
            db.rollback()

    return updated


def _refresh_single_channel_metadata(channel: Channel, db: Session) -> None:
    """Fetch and update metadata + images for a single channel."""
    # Fetch channel name / canonical ID via yt-dlp
    channel_meta = fetch_channel_metadata(channel.youtube_channel_id)

    # Update display name if we still have a raw ID/handle as the name
    resolved_name = channel_meta.get("name")
    if resolved_name and channel.name in (
        channel.youtube_channel_id,
        f"@{channel.youtube_channel_id}",
        channel.youtube_channel_id.lstrip("@"),
    ):
        channel.name = resolved_name

    # Canonicalize youtube_channel_id to the UC ID
    canonical_id = channel_meta.get("channel_id")
    if (
        canonical_id
        and canonical_id.startswith("UC")
        and canonical_id != channel.youtube_channel_id
    ):
        existing = db.execute(
            select(Channel).where(
                Channel.youtube_channel_id == canonical_id,
                Channel.id != channel.id,
            )
        ).scalar_one_or_none()
        if not existing:
            logger.info(
                "Canonicalizing channel %s: %s -> %s",
                channel.id,
                channel.youtube_channel_id,
                canonical_id,
            )
            channel.youtube_channel_id = canonical_id

    # Fetch avatar & banner images
    images = fetch_channel_images(channel.youtube_channel_id)
    if images:
        if images.get("avatar_url"):
            channel.avatar_url = images["avatar_url"]
        if images.get("banner_url"):
            channel.banner_url = images["banner_url"]

    channel.metadata_refreshed_at = utcnow_naive()
    db.commit()


def _determine_auto_downloads(
    new_video_ids: list[str],
    channel_id: str,
    db: Session,
    had_initial_poll: bool,
) -> list[str]:
    """Determine which new videos should be auto-downloaded based on subscriber tracking modes.

    A video qualifies for FUTURE_ONLY auto-download when its row was created
    during this poll (it's in new_video_ids), the channel already had a
    completed initial poll (so this isn't back-catalog ingestion), and the
    row was created after the subscription.
    """
    if not had_initial_poll:
        # Initial poll: everything discovered is back catalog. Catalog only.
        return []

    # Get all subscribers and their tracking modes
    sub_result = db.execute(
        select(UserSubscription).where(UserSubscription.channel_id == channel_id)
    )
    subscriptions = sub_result.scalars().all()

    videos = (
        db.execute(select(Video).where(Video.id.in_(new_video_ids))).scalars().all()
    )

    auto_download_set: set[str] = set()
    for sub in subscriptions:
        if sub.tracking_mode == "ALL_VIDEOS":
            # ALL_VIDEOS mode: never auto-download, just catalog
            continue

        # FUTURE_ONLY (default): auto-download videos cataloged after the
        # user subscribed. Compare as naive UTC.
        subscribed_at = _as_naive_utc(sub.subscribed_at)
        if subscribed_at is None:
            continue
        for video in videos:
            if video.status != "CATALOGED":
                continue
            created_at = _as_naive_utc(video.created_at)
            if created_at and created_at > subscribed_at:
                auto_download_set.add(video.id)

    # Set auto-download candidates to PENDING
    for video in videos:
        if video.id in auto_download_set and video.status == "CATALOGED":
            video.status = "PENDING"

    return list(auto_download_set)


def _emit_new_episode_events(channel_id: str, videos: list[dict], db: Session) -> None:
    """Notify every subscriber of a channel's genuinely new episodes.

    For each (subscriber, new video) we publish the live WebSocket event (for an
    open app) and, best-effort, send a push notification (for a closed one) so
    the client can deep-link to the video. Everything is best-effort: a failure
    (Redis down, push gateway unreachable/disabled) must never break a poll, so
    the whole emit is wrapped and only logged, and ``send_to_users`` itself never
    raises and no-ops when push is disabled.
    """
    try:
        channel = db.get(Channel, channel_id)
        channel_name = channel.name if channel is not None else None
        subscriber_ids = [
            row[0]
            for row in db.execute(
                select(UserSubscription.user_id).where(
                    UserSubscription.channel_id == channel_id
                )
            ).all()
        ]
        for user_id in subscriber_ids:
            for video in videos:
                publish_new_episode(
                    video["id"],
                    user_id,
                    channel_id=channel_id,
                    title=video["title"],
                    youtube_video_id=video["youtube_video_id"],
                )
                # Push for a backgrounded/closed app. Target the user id (the
                # gateway rejects raw tokens for tenant keys); data carries the
                # video id so the client can deep-link to it.
                send_to_users(
                    [user_id],
                    channel_name or "New episode",
                    video["title"],
                    {"type": "new_episode", "video_id": video["id"]},
                )
    except Exception:
        logger.exception(
            "Failed to publish new_episode events for channel %s", channel_id
        )


def _ensure_user_refs_bulk(video_ids: list[str], channel_id: str, db: Session) -> None:
    """Ensure every subscriber of the channel has a UserVideoRef per video.

    Batched to a bounded number of queries regardless of how many videos this
    poll touched or how many subscribers the channel has: one query for the
    subscriber ids and one for the refs that already exist across the whole
    (subscriber x video) set, then a single set-difference insert. Replaces the
    per-video, per-subscriber N+1 the old per-video helper issued; mirrors the
    batched pattern in app/api/channels.py::_ensure_refs_for_channel.

    Keeps the prior create-if-missing semantics: an already-present ref is left
    untouched, so a soft-removed ref (removed_at set) is not resurrected.
    """
    unique_video_ids = list(dict.fromkeys(video_ids))
    if not unique_video_ids:
        return

    subscriber_ids = [
        row[0]
        for row in db.execute(
            select(UserSubscription.user_id).where(
                UserSubscription.channel_id == channel_id
            )
        ).all()
    ]
    if not subscriber_ids:
        return

    existing_pairs = set(
        db.execute(
            select(UserVideoRef.user_id, UserVideoRef.video_id).where(
                UserVideoRef.user_id.in_(subscriber_ids),
                UserVideoRef.video_id.in_(unique_video_ids),
            )
        ).all()
    )

    # New episodes of followed channels are cached quietly for subscribers (they
    # will likely open them) — a background prefetch, not a visible download.
    for user_id in subscriber_ids:
        for video_id in unique_video_ids:
            if (user_id, video_id) not in existing_pairs:
                db.add(
                    UserVideoRef(
                        user_id=user_id, video_id=video_id, kind=REF_KIND_CACHE
                    )
                )
