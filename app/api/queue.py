import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.database import get_db
from app.models.channel import Channel
from app.models.user import User
from app.models.user_queue import UserQueue
from app.models.user_video_ref import UserVideoRef
from app.models.video import Video
from app.schemas.video import VideoOut, VideoSearchPage
from app.utils.pagination import decode_cursor, encode_cursor
from app.utils.time import utcnow_naive

# Video-scoped mutations live under /api/videos (alongside /download, /cancel,
# etc.); the listing is the top-level collection /api/queue. Both share the
# "queue" tag so they group together in the OpenAPI docs.
router = APIRouter(tags=["queue"])
logger = logging.getLogger(__name__)


@router.post("/api/videos/{video_id}/queue")
async def add_to_queue(
    video_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Append a video to the caller's watch-later queue.

    Idempotent: re-adding a video already in the queue is a no-op that keeps its
    original position (``added_at``). 404 if the video does not exist.
    """
    exists = await db.scalar(select(Video.id).where(Video.id == video_id))
    if exists is None:
        raise HTTPException(status_code=404, detail="Video not found")

    stmt = (
        sqlite_insert(UserQueue)
        .values(user_id=user.id, video_id=video_id, added_at=utcnow_naive())
        .on_conflict_do_nothing(index_elements=["user_id", "video_id"])
    )
    await db.execute(stmt)
    await db.commit()
    return {"detail": "Added to queue", "video_id": video_id}


@router.delete("/api/videos/{video_id}/queue")
async def remove_from_queue(
    video_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Remove a video from the caller's queue.

    Idempotent: succeeds with 200 whether or not the video was queued (and even
    if the video does not exist), since the end state — not in the queue — is the
    same either way.
    """
    await db.execute(
        delete(UserQueue).where(
            UserQueue.user_id == user.id,
            UserQueue.video_id == video_id,
        )
    )
    await db.commit()
    return {"detail": "Removed from queue", "video_id": video_id}


@router.get("/api/queue", response_model=VideoSearchPage)
async def list_queue(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VideoSearchPage:
    """The caller's watch-later queue, oldest-queued first.

    Returns the cursor-paginated ``VideoSearchPage`` envelope (the project's
    default; the offset ``VideoPagination`` is kept only for the legacy
    channel-videos contract). Queue rows carry a natural ``(added_at, video_id)``
    keyset that maps directly onto the shared cursor helpers. Each item's watch
    progress is filled from the caller's active ref when one exists, so a queued
    video the user has never opened still appears (with default progress).
    """
    sort_key = UserQueue.added_at

    filters = [UserQueue.user_id == user.id]
    total = (
        await db.scalar(select(func.count()).select_from(UserQueue).where(*filters))
        or 0
    )

    page_filters = list(filters)
    if cursor:
        decoded = decode_cursor(cursor)
        if decoded is None:
            raise HTTPException(status_code=400, detail="Invalid cursor")
        cur_sort, cur_id = decoded
        page_filters.append(
            or_(
                sort_key > cur_sort,
                and_(sort_key == cur_sort, UserQueue.video_id > cur_id),
            )
        )

    # Fetch one extra row to detect whether another page exists. The LEFT JOIN to
    # the caller's active ref is what lets a queued-but-never-watched video still
    # come back (with null/default progress).
    result = await db.execute(
        select(UserQueue, Video, Channel, UserVideoRef)
        .join(Video, UserQueue.video_id == Video.id)
        .join(Channel, Video.channel_id == Channel.id)
        .outerjoin(
            UserVideoRef,
            and_(
                UserVideoRef.video_id == UserQueue.video_id,
                UserVideoRef.user_id == user.id,
                UserVideoRef.removed_at.is_(None),
            ),
        )
        .where(*page_filters)
        .order_by(sort_key.asc(), UserQueue.video_id.asc())
        .limit(limit + 1)
    )
    rows = result.all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    items = [
        VideoOut(
            id=video.id,
            youtube_video_id=video.youtube_video_id,
            channel_id=video.channel_id,
            title=video.title,
            duration_seconds=video.duration_seconds,
            uploaded_at=video.uploaded_at,
            file_size_bytes=video.file_size_bytes or 0,
            status=video.status,
            preview_status=video.preview_status,
            thumbnail_url=f"/data/thumbnails/{video.youtube_video_id}.jpg",
            watch_position_seconds=ref.watch_position_seconds if ref else 0,
            is_watched=ref.is_watched if ref else False,
            last_watched_at=ref.last_watched_at if ref else None,
            channel_name=channel.name,
        )
        for _queue_row, video, channel, ref in rows
    ]

    next_cursor = None
    if has_more and rows:
        last_queue_row = rows[-1][0]
        next_cursor = encode_cursor(last_queue_row.added_at, last_queue_row.video_id)

    return VideoSearchPage(items=items, total=total, next_cursor=next_cursor)
