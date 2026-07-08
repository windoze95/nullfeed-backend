"""Per-channel content-type gate: user_subscriptions.hidden_content_types

Revision ID: 017_subscription_hidden_content_types
Revises: 016_video_content_type
Create Date: 2026-07-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "017_subscription_hidden_content_types"
down_revision: Union[str, None] = "016_video_content_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_subscriptions",
        sa.Column("hidden_content_types", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_subscriptions", "hidden_content_types")
