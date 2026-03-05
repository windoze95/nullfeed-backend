"""Add metadata_refreshed_at to channels

Revision ID: 004_add_metadata_refreshed_at
Revises: 003_add_preview_fields
Create Date: 2026-03-04
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "004_add_metadata_refreshed_at"
down_revision: Union[str, None] = "003_add_preview_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "channels",
        sa.Column("metadata_refreshed_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("channels", "metadata_refreshed_at")
