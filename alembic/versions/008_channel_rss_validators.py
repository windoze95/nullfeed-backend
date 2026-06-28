"""Add channels.rss_etag / rss_last_modified for conditional-GET polling

Revision ID: 008_channel_rss_validators
Revises: 007_download_heartbeat
Create Date: 2026-06-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008_channel_rss_validators"
down_revision: Union[str, None] = "007_download_heartbeat"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # HTTP cache validators for each channel's Atom upload feed. Nullable so the
    # columns add without a backfill: existing channels get NULL, which sends no
    # conditional headers on the next poll and simply repopulates the validators
    # from that response.
    op.add_column("channels", sa.Column("rss_etag", sa.String(255), nullable=True))
    op.add_column(
        "channels", sa.Column("rss_last_modified", sa.String(255), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("channels", "rss_last_modified")
    op.drop_column("channels", "rss_etag")
