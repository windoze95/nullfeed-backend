"""Distinguish library vs cache refs: user_video_refs.kind

Revision ID: 013_user_video_refs_kind
Revises: 012_channel_websub_expires_at
Create Date: 2026-06-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "013_user_video_refs_kind"
down_revision: Union[str, None] = "012_channel_websub_expires_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing refs were all created by explicit intent (download/subscription),
    # so they backfill as LIBRARY. New play-cache refs are written as CACHE.
    op.add_column(
        "user_video_refs",
        sa.Column(
            "kind",
            sa.String(length=20),
            nullable=False,
            server_default="LIBRARY",
        ),
    )


def downgrade() -> None:
    op.drop_column("user_video_refs", "kind")
