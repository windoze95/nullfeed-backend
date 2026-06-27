"""Hot-path indexes: user_subscriptions.channel_id and user_video_refs(video_id, removed_at)

Revision ID: 006_hotpath_indexes
Revises: 005_profiles_and_hardening
Create Date: 2026-06-27
"""

from typing import Sequence, Union

from alembic import op

revision: str = "006_hotpath_indexes"
down_revision: Union[str, None] = "005_profiles_and_hardening"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Subscriber lookups filter user_subscriptions by channel_id alone, which the
    # (user_id, channel_id) primary key cannot serve (user_id is the lead column).
    op.create_index(
        "ix_user_subscriptions_channel_id", "user_subscriptions", ["channel_id"]
    )

    # Orphan cleanup filters user_video_refs by (video_id, removed_at IS NULL).
    # The standalone removed_at index is never used as a lead column by any query
    # (every removed_at filter is paired with user_id or video_id), so replace it
    # with the composite that the orphan check can actually use.
    op.drop_index("ix_user_video_refs_removed", table_name="user_video_refs")
    op.create_index(
        "ix_user_video_refs_video_id_removed",
        "user_video_refs",
        ["video_id", "removed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_video_refs_video_id_removed", table_name="user_video_refs")
    op.create_index("ix_user_video_refs_removed", "user_video_refs", ["removed_at"])
    op.drop_index("ix_user_subscriptions_channel_id", table_name="user_subscriptions")
