"""Cached channel embedding vectors for the discovery pipeline.

The provider-selectable Discover pipeline embeds each channel's text profile
(name/description + recent video titles) and caches the vector here. Rows are
keyed by (youtube_channel_id, model) because vectors from different embedding
models are not comparable: switching provider/model writes fresh rows under
the new key instead of migrating old ones.

Revision ID: 019_channel_embeddings
Revises: 018_backfill_content_type_from_reason
Create Date: 2026-07-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "019_channel_embeddings"
down_revision: Union[str, None] = "018_backfill_content_type_from_reason"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "channel_embeddings",
        sa.Column("youtube_channel_id", sa.String(length=255), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("handle", sa.String(length=255), nullable=True),
        sa.Column("vector", sa.JSON(), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("youtube_channel_id", "model"),
    )


def downgrade() -> None:
    op.drop_table("channel_embeddings")
