"""Initial schema

Revision ID: 001_initial
Revises:
Create Date: 2026-03-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Users
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("avatar_url", sa.String(512), nullable=True),
        sa.Column("pin_hash", sa.String(255), nullable=True),
        sa.Column("is_admin", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )

    # Channels
    op.create_table(
        "channels",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("youtube_channel_id", sa.String(255), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False, unique=True),
        sa.Column("description", sa.Text, nullable=True, server_default=""),
        sa.Column("banner_url", sa.String(512), nullable=True),
        sa.Column("avatar_url", sa.String(512), nullable=True),
        sa.Column("last_checked_at", sa.DateTime, nullable=True),
    )

    # Videos
    op.create_table(
        "videos",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("youtube_video_id", sa.String(255), nullable=False, unique=True),
        sa.Column("channel_id", sa.String(36), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("duration_seconds", sa.Integer, nullable=True, server_default="0"),
        sa.Column("uploaded_at", sa.DateTime, nullable=True),
        sa.Column("file_path", sa.String(1024), nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger, nullable=True, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("metadata_json", sa.JSON, nullable=True),
    )
    op.create_index("ix_videos_channel_id", "videos", ["channel_id"])
    op.create_index("ix_videos_status", "videos", ["status"])

    # UserSubscriptions
    op.create_table(
        "user_subscriptions",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("channel_id", sa.String(36), sa.ForeignKey("channels.id"), primary_key=True),
        sa.Column("subscribed_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("retention_policy", sa.String(20), nullable=False, server_default="KEEP_ALL"),
        sa.Column("retention_count", sa.Integer, nullable=True),
    )

    # UserVideoRefs
    op.create_table(
        "user_video_refs",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("video_id", sa.String(36), sa.ForeignKey("videos.id"), primary_key=True),
        sa.Column("watch_position_seconds", sa.Integer, nullable=False, server_default="0"),
        sa.Column("is_watched", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("added_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("removed_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_user_video_refs_removed", "user_video_refs", ["removed_at"])

    # AI Recommendations cache
    op.create_table(
        "recommendations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("channel_name", sa.String(255), nullable=False),
        sa.Column("youtube_channel_id", sa.String(255), nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("dismissed", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_recommendations_user_id", "recommendations", ["user_id"])


def downgrade() -> None:
    op.drop_table("recommendations")
    op.drop_table("user_video_refs")
    op.drop_table("user_subscriptions")
    op.drop_table("videos")
    op.drop_table("channels")
    op.drop_table("users")
