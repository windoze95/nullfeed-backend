"""Add preview_file_path and preview_status to videos

Revision ID: 003_add_preview_fields
Revises: 002_add_tracking_mode
Create Date: 2026-03-04
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "003_add_preview_fields"
down_revision: Union[str, None] = "002_add_tracking_mode"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "videos",
        sa.Column("preview_file_path", sa.String(1024), nullable=True),
    )
    op.add_column(
        "videos",
        sa.Column("preview_status", sa.String(20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("videos", "preview_status")
    op.drop_column("videos", "preview_file_path")
