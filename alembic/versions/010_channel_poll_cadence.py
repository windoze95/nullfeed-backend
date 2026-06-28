"""Adaptive per-channel poll cadence: channels.next_poll_at / poll_interval_minutes

Revision ID: 010_channel_poll_cadence
Revises: 009_user_queue
Create Date: 2026-06-27
"""

from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "010_channel_poll_cadence"
down_revision: Union[str, None] = "009_user_queue"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Per-channel adaptive poll cadence. next_poll_at gates which channels the
    # beat polls (only those due, next_poll_at <= now); poll_interval_minutes is
    # the current spacing, adjusted by multiplicative backoff after each poll.
    #
    # next_poll_at backfills existing rows to "now" so every channel is
    # immediately due exactly once and then settles onto its own schedule. The
    # default is a constant captured at migration time rather than CURRENT_
    # TIMESTAMP because SQLite forbids a non-constant default on ADD COLUMN; the
    # ORM supplies its own per-row default for future inserts, so this constant
    # only ever applies to the rows that exist right now.
    now_literal = (
        datetime.now(timezone.utc)
        .replace(microsecond=0, tzinfo=None)
        .isoformat(sep=" ")
    )
    op.add_column(
        "channels",
        sa.Column(
            "next_poll_at",
            sa.DateTime(),
            nullable=False,
            server_default=now_literal,
        ),
    )
    # Seed the interval at the floor default so a freshly migrated channel starts
    # responsive and only backs off if its polls keep coming up empty.
    op.add_column(
        "channels",
        sa.Column(
            "poll_interval_minutes",
            sa.Integer(),
            nullable=False,
            server_default="15",
        ),
    )
    # Serves the beat's WHERE next_poll_at <= now due-check.
    op.create_index("ix_channels_next_poll_at", "channels", ["next_poll_at"])


def downgrade() -> None:
    op.drop_index("ix_channels_next_poll_at", table_name="channels")
    op.drop_column("channels", "poll_interval_minutes")
    op.drop_column("channels", "next_poll_at")
