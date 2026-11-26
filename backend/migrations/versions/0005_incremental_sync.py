"""Event timestamps for incremental sync

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-25

`sync --incremental` only lists matches for tracked events that are still ongoing. An
event's last matches (a grand final) can finish after its last incremental sync and
before discover marks it completed, so each event gets one last check after that:

completed_at       when an event list first showed the event as completed after we had
                   seen it in another state (or only as a hand-tracked placeholder).
                   NULL for events first discovered already completed: the backfill
                   covers those.
matches_listed_at  when sync last listed the event's matches.

An event with matches_listed_at earlier than completed_at (or never listed) still needs
that last check.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE events
            ADD COLUMN completed_at      timestamptz,
            ADD COLUMN matches_listed_at timestamptz;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE events DROP COLUMN matches_listed_at, DROP COLUMN completed_at;")
