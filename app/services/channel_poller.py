import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.channel import Channel
from app.models.subscription import UserSubscription
from app.models.user_video_ref import UserVideoRef
from app.models.video import Video
from app.services.download_manager import fetch_channel_metadata, fetch_channel_videos

logger = logging.getLogger(__name__)


def poll_all_channels(db: Session) -> None:
    """Poll all channels that have at least one subscriber."""
    result = db.execute(
        select(Channel.id).join(
            UserSubscription, UserSubscription.channel_id == Channel.id
        ).distinct()
    )
    channel_ids = [row[0] for row in result.all()]
    logger.info("Polling %d channels", len(channel_ids))

    for channel_id in channel_ids:
        try:
            poll_single_channel(channel_id, db)
        except Exception:
            logger.exception("Error polling channel %s", channel_id)


def poll_single_channel(channel_id: str, db: Session) -> list[str]:
    """
    Poll a single channel for new videos.
    Returns list of new video IDs that were enqueued for download.
    """
    channel = db.get(Channel, channel_id)
    if not channel:
        logger.warning("Channel %s not found", channel_id)
        return []

    # Update channel metadata if name is still the raw ID
    if channel.name == channel.youtube_channel_id:
        meta = fetch_channel_metadata(channel.youtube_channel_id)
        channel.name = meta.get("name", channel.name)
        channel.description = meta.get("description", channel.description)
        if meta.get("channel_id") and meta["channel_id"] != channel.youtube_channel_id:
            channel.youtube_channel_id = meta["channel_id"]

    # Fetch latest videos from YouTube
    yt_videos = fetch_channel_videos(channel.youtube_channel_id)

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

        # Create new video record as PENDING
        video = Video(
            id=str(uuid.uuid4()),
            youtube_video_id=yt_video_id,
            channel_id=channel_id,
            title=yt_vid.get("title", yt_video_id),
            duration_seconds=yt_vid.get("duration_seconds", 0),
            status="PENDING",
        )
        db.add(video)
        db.flush()

        # Create user video refs for all subscribers
        _ensure_user_refs(video, channel_id, db)

        new_video_ids.append(video.id)
        logger.info("New video discovered: %s (%s)", yt_video_id, video.title)

    channel.last_checked_at = datetime.now(timezone.utc)
    db.commit()

    return new_video_ids


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
