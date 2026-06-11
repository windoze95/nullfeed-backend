"""Profiles and hardening: sessions table, last_watched_at, downloaded_at

Revision ID: 005_profiles_and_hardening
Revises: 004_add_metadata_refreshed_at
Create Date: 2026-06-11
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "005_profiles_and_hardening"
down_revision: Union[str, None] = "004_add_metadata_refreshed_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Persistent auth sessions (token_hash is SHA-256 hex of the raw token)
    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime, nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "last_seen_at", sa.DateTime, nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    op.add_column(
        "user_video_refs",
        sa.Column("last_watched_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "videos",
        sa.Column("downloaded_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("videos", "downloaded_at")
    op.drop_column("user_video_refs", "last_watched_at")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
