"""Orphan cleanup tests: removing the last ref resets the video row (design 1.4)."""

import os

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import async_session_factory
from app.models.user_video_ref import UserVideoRef
from app.models.video import Video
from tests.helpers import seed_channel, seed_ref, seed_video

pytestmark = pytest.mark.asyncio


def _write_file(path: str, data: bytes = b"data") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


async def test_remove_last_ref_deletes_files_and_resets_video(client, make_user):
    user, headers = await make_user()

    rel_path = "chan/orphan.mp4"
    preview_rel_path = "chan/orphan.preview.mp4"
    full_path = os.path.join(settings.media_path, rel_path)
    preview_full_path = os.path.join(settings.media_path, preview_rel_path)
    _write_file(full_path)
    _write_file(preview_full_path)

    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(
            db,
            channel,
            status="COMPLETE",
            file_path=rel_path,
            preview_file_path=preview_rel_path,
            preview_status="READY",
        )
        await seed_ref(db, user["id"], video.id)

    thumb_path = os.path.join(settings.thumbnails_path, f"{video.youtube_video_id}.jpg")
    _write_file(thumb_path)

    resp = await client.delete(f"/api/videos/{video.id}", headers=headers)
    assert resp.status_code == 200

    assert not os.path.exists(full_path)
    assert not os.path.exists(preview_full_path)
    assert not os.path.exists(thumb_path)

    async with async_session_factory() as db:
        v = (await db.execute(select(Video).where(Video.id == video.id))).scalar_one()
        assert v.status == "CATALOGED"
        assert v.file_path is None
        assert v.file_size_bytes is None
        assert v.preview_file_path is None
        assert v.preview_status is None


async def test_orphan_cleanup_skipped_while_other_refs_active(client, make_user):
    user_a, headers_a = await make_user("A")
    user_b, _ = await make_user("B")

    rel_path = "chan/shared.mp4"
    full_path = os.path.join(settings.media_path, rel_path)
    _write_file(full_path)

    async with async_session_factory() as db:
        channel = await seed_channel(db)
        video = await seed_video(db, channel, status="COMPLETE", file_path=rel_path)
        await seed_ref(db, user_a["id"], video.id)
        await seed_ref(db, user_b["id"], video.id)

    resp = await client.delete(f"/api/videos/{video.id}", headers=headers_a)
    assert resp.status_code == 200

    # The other user still references the video: the file must survive.
    assert os.path.exists(full_path)
    async with async_session_factory() as db:
        v = (await db.execute(select(Video).where(Video.id == video.id))).scalar_one()
        assert v.status == "COMPLETE"
        assert v.file_path == rel_path

        ref_a = (
            await db.execute(
                select(UserVideoRef).where(
                    UserVideoRef.user_id == user_a["id"],
                    UserVideoRef.video_id == video.id,
                )
            )
        ).scalar_one()
        assert ref_a.removed_at is not None
