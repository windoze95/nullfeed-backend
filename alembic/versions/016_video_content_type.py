"""What kind of media a video is, for badges + per-channel gating: videos.content_type

Revision ID: 016_video_content_type
Revises: 015_video_unplayable_reason
Create Date: 2026-07-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "016_video_content_type"
down_revision: Union[str, None] = "015_video_unplayable_reason"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "videos", sa.Column("content_type", sa.String(length=20), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("videos", "content_type")
