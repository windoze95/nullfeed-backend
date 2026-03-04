"""Add tracking_mode to user_subscriptions and CATALOGED video status

Revision ID: 002_add_tracking_mode
Revises: 001_initial
Create Date: 2026-03-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002_add_tracking_mode"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_subscriptions",
        sa.Column("tracking_mode", sa.String(20), nullable=False, server_default="FUTURE_ONLY"),
    )

    # Update existing PENDING videos that have no file on disk to CATALOGED
    op.execute(
        "UPDATE videos SET status = 'CATALOGED' WHERE status = 'PENDING' AND file_path IS NULL"
    )


def downgrade() -> None:
    # Revert CATALOGED videos back to PENDING
    op.execute(
        "UPDATE videos SET status = 'PENDING' WHERE status = 'CATALOGED'"
    )

    op.drop_column("user_subscriptions", "tracking_mode")
