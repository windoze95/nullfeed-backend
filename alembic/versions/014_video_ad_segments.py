"""Sponsor/ad segments for client-side skipping: videos.ad_segments(_status)

Revision ID: 014_video_ad_segments
Revises: 013_user_video_refs_kind
Create Date: 2026-06-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "014_video_ad_segments"
down_revision: Union[str, None] = "013_user_video_refs_kind"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("videos", sa.Column("ad_segments", sa.JSON(), nullable=True))
    op.add_column(
        "videos", sa.Column("ad_segments_status", sa.String(length=20), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("videos", "ad_segments_status")
    op.drop_column("videos", "ad_segments")
