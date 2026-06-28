"""Track WebSub lease expiry: channels.websub_expires_at

Revision ID: 012_channel_websub_expires_at
Revises: 011_videos_channel_uploaded_index
Create Date: 2026-06-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "012_channel_websub_expires_at"
down_revision: Union[str, None] = "011_videos_channel_uploaded_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable: NULL means no active WebSub (PubSubHubbub) subscription — either
    # the feature is disabled or the channel was never subscribed — so existing
    # rows backfill to NULL and the subscribe beat task picks them up as due.
    op.add_column(
        "channels",
        sa.Column("websub_expires_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("channels", "websub_expires_at")
