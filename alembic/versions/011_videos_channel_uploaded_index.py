"""Composite index videos(channel_id, uploaded_at) for the /new-episodes window query

Revision ID: 011_videos_channel_uploaded_index
Revises: 010_channel_poll_cadence
Create Date: 2026-06-28
"""

from typing import Sequence, Union

from alembic import op

revision: str = "011_videos_channel_uploaded_index"
down_revision: Union[str, None] = "010_channel_poll_cadence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # /new-episodes picks the newest unwatched video per channel with a window
    # function (PARTITION BY channel_id ORDER BY uploaded_at DESC). This
    # composite index lets SQLite satisfy that partition+order directly instead
    # of scanning every video in the subscribed library.
    op.create_index(
        "ix_videos_channel_id_uploaded_at",
        "videos",
        ["channel_id", "uploaded_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_videos_channel_id_uploaded_at", table_name="videos")
