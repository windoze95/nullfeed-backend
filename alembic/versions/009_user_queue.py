"""User watch-later queue

Revision ID: 009_user_queue
Revises: 008_channel_rss_validators
Create Date: 2026-06-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "009_user_queue"
down_revision: Union[str, None] = "008_channel_rss_validators"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Per-user watch-later queue. Pure bookmark (no download ref-count), ordered
    # oldest-first by added_at with video_id as a stable tiebreaker.
    op.create_table(
        "user_queue",
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True
        ),
        sa.Column(
            "video_id", sa.String(36), sa.ForeignKey("videos.id"), primary_key=True
        ),
        sa.Column(
            "added_at", sa.DateTime, nullable=False, server_default=sa.func.now()
        ),
    )
    # Serves the listing's WHERE user_id + ORDER BY added_at; the (user_id,
    # video_id) primary key cannot order by added_at.
    op.create_index("ix_user_queue_user_added", "user_queue", ["user_id", "added_at"])


def downgrade() -> None:
    op.drop_index("ix_user_queue_user_added", table_name="user_queue")
    op.drop_table("user_queue")
