"""Backfill videos.content_type from the unplayable_reason already stored

Videos cataloged before the content_type column existed have content_type NULL,
so they read as "regular" and can't be filtered by kind. But members-only /
premium / age-restricted / premiere videos already carry an unplayable_reason
label (set at catalog time from YouTube's availability/live badges). This is a
pure data backfill — no yt-dlp — that copies that label into content_type so
existing content is immediately filterable. Types with no such label (Shorts,
livestreams, plain videos) are left NULL and picked up by discovery /
reclassification.

Revision ID: 018_backfill_content_type_from_reason
Revises: 017_subscription_hidden_content_types
Create Date: 2026-07-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "018_backfill_content_type_from_reason"
down_revision: Union[str, None] = "017_subscription_hidden_content_types"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    # members_only / premium / age_restricted map to the same content_type name.
    conn.execute(
        sa.text(
            "UPDATE videos SET content_type = unplayable_reason "
            "WHERE content_type IS NULL AND unplayable_reason IN "
            "('members_only', 'premium', 'age_restricted')"
        )
    )
    # An unaired premiere is labeled 'upcoming' but is content type 'premiere'.
    conn.execute(
        sa.text(
            "UPDATE videos SET content_type = 'premiere' "
            "WHERE content_type IS NULL AND unplayable_reason = 'upcoming'"
        )
    )


def downgrade() -> None:
    # Data backfill: no-op. The content_type column (added in 016) and every
    # value remain; there's no reliable way to tell a backfilled value from one
    # a later catalog/reclassify pass set to the same kind, so we don't guess.
    pass
