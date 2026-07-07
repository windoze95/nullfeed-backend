"""Why YouTube refuses a video, for client banners: videos.unplayable_reason

Revision ID: 015_video_unplayable_reason
Revises: 014_video_ad_segments
Create Date: 2026-07-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "015_video_unplayable_reason"
down_revision: Union[str, None] = "014_video_ad_segments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "videos", sa.Column("unplayable_reason", sa.String(length=32), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("videos", "unplayable_reason")
