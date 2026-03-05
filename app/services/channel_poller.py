import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.channel import Channel
from app.models.subscription import UserSubscription
from app.models.user_video_ref import UserVideoRef
from app.models.video import Video
from app.services.download_manager import (
    fetch_channel_images,
    fetch_channel_metadata,
    fetch_channel_videos,
)

logger = logging.getLogger(__name__)


def poll_all_channels(db: Session) -> list[str]:
    """Poll all channels that have at least one subscriber.
    Returns aggregated list of auto-download video IDs."""
    result = db.execute(
        select(Channel.id)
        .join(UserSubscription, UserSubscription.channel_id == Channel.id)
        .distinct()
    )
    channel_ids = [row[0] for row in result.all()]
    logger.info("Polling %d channels", len(channel_ids))

    all_auto_download_ids: list[str] = []
    for channel_id in channel_ids:
        try:
            poll_result = poll_single_channel(channel_id, db)
            all_auto_download_ids.extend(poll_result["auto_download_ids"])
        except Exception:
            logger.exception("Error polling channel %s", channel_id)

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

    # Fetch latest videos from YouTube
    fetch_result = fetch_channel_videos(channel.youtube_channel_id)
    yt_videos = fetch_result["videos"]

    cataloged_ids: list[str] = []
    new_video_ids: list[str] = []

    for yt_vid in yt_videos:
        yt_video_id = yt_vid["youtube_video_id"]
        if not yt_video_id:
            continue

        # Check if video already exists
        existing = db.execute(
            select(Video).where(Video.youtube_video_id == yt_video_id)
        ).scalar_one_or_none()

        if existing:
            # Video exists; ensure all subscribers have a reference.
            _ensure_user_refs(existing, channel_id, db)
            continue

        # Parse upload_date into uploaded_at
        uploaded_at = None
        if yt_vid.get("upload_date"):
            try:
                uploaded_at = datetime.strptime(
                    yt_vid["upload_date"], "%Y%m%d"
                ).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass

        # Create new video record as CATALOGED (not PENDING)
        video = Video(
            id=str(uuid.uuid4()),
            youtube_video_id=yt_video_id,
            channel_id=channel_id,
            title=yt_vid.get("title", yt_video_id),
            duration_seconds=yt_vid.get("duration_seconds", 0),
            uploaded_at=uploaded_at,
            status="CATALOGED",
        )
        db.add(video)
        db.flush()

        # Create user video refs for all subscribers
        _ensure_user_refs(video, channel_id, db)

        new_video_ids.append(video.id)
        cataloged_ids.append(video.id)
        logger.info("New video cataloged: %s (%s)", yt_video_id, video.title)

    # Determine auto-download candidates based on subscriber tracking modes
    auto_download_ids: list[str] = []
    if new_video_ids:
        auto_download_ids = _determine_auto_downloads(new_video_ids, channel_id, db)

    channel.last_checked_at = datetime.now(timezone.utc)
    db.commit()

    return {"cataloged_ids": cataloged_ids, "auto_download_ids": auto_download_ids}


def refresh_stale_channel_metadata(db: Session) -> int:
    """Refresh metadata for channels with missing or stale images.

    Returns the number of channels updated.
    """
    from app.config import settings

    staleness_threshold = datetime.now(timezone.utc) - timedelta(
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

    channel.metadata_refreshed_at = datetime.now(timezone.utc)
    db.commit()


def _determine_auto_downloads(
    new_video_ids: list[str], channel_id: str, db: Session
) -> list[str]:
    """Determine which new videos should be auto-downloaded based on subscriber tracking modes."""
    # Get all subscribers and their tracking modes
    sub_result = db.execute(
        select(UserSubscription).where(UserSubscription.channel_id == channel_id)
    )
    subscriptions = sub_result.scalars().all()

    # If any subscriber has FUTURE_ONLY mode, check upload dates vs subscription dates
    auto_download_set: set[str] = set()

    for sub in subscriptions:
        if sub.tracking_mode == "ALL_VIDEOS":
            # ALL_VIDEOS mode: never auto-download, just catalog
            continue

        # FUTURE_ONLY (default): auto-download if uploaded_at > subscribed_at
        for video_id in new_video_ids:
            video = db.get(Video, video_id)
            if not video or video.status != "CATALOGED":
                continue

            if video.uploaded_at and sub.subscribed_at:
                if video.uploaded_at > sub.subscribed_at:
                    auto_download_set.add(video_id)
            # If upload_date is unknown, do NOT auto-download for FUTURE_ONLY
            # (conservative: leave as cataloged to avoid back-catalog spam)

    # Set auto-download candidates to PENDING
    for video_id in auto_download_set:
        video = db.get(Video, video_id)
        if video and video.status == "CATALOGED":
            video.status = "PENDING"

    return list(auto_download_set)


def _ensure_user_refs(video: Video, channel_id: str, db: Session) -> None:
    """Ensure all subscribers of a channel have a UserVideoRef for this video."""
    sub_result = db.execute(
        select(UserSubscription.user_id).where(
            UserSubscription.channel_id == channel_id
        )
    )
    subscriber_ids = [row[0] for row in sub_result.all()]

    for user_id in subscriber_ids:
        existing_ref = db.execute(
            select(UserVideoRef).where(
                UserVideoRef.user_id == user_id,
                UserVideoRef.video_id == video.id,
            )
        ).scalar_one_or_none()

        if not existing_ref:
            ref = UserVideoRef(user_id=user_id, video_id=video.id)
            db.add(ref)
