"""Add videos.download_heartbeat_at for the stuck-download reaper

Revision ID: 007_download_heartbeat
Revises: 006_hotpath_indexes
Create Date: 2026-06-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007_download_heartbeat"
down_revision: Union[str, None] = "006_hotpath_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Liveness timestamp for in-flight downloads. Nullable so the column can be
    # added without backfilling: existing DOWNLOADING/CANCELLING rows get NULL,
    # which the reaper treats as stale (those rows predate any live worker).
    op.add_column(
        "videos",
        sa.Column("download_heartbeat_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("videos", "download_heartbeat_at")
