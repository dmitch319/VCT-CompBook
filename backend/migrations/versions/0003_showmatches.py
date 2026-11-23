"""Keep showmatches out of the database

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-22

Tier-1 events include showmatches and all-star games (content creators, mixed teams).
They aren't competitive, their players often can't be resolved to vlr.gg IDs, and they
would skew comp statistics. vlr.gg lists them with stage 'Showmatch' (series vary:
'Showmatch', 'All-Star Match', 'Main Event', 'Override').

The pipeline drops them when it reads an event's match list (parse.is_showmatch), so
they never get a row; this CHECK constraint is the backstop, so nothing can store one.
The pattern must stay in step with parse.SHOWMATCH_PATTERN.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHOWMATCH = r"'show\s*match|all[- ]star'"


def upgrade() -> None:
    op.execute(
        f"""
        -- Showmatches stored before this migration (and their maps, via ON DELETE CASCADE)
        DELETE FROM matches
        WHERE coalesce(stage, '') ~* {_SHOWMATCH} OR coalesce(series, '') ~* {_SHOWMATCH};

        ALTER TABLE matches ADD CONSTRAINT matches_not_showmatch CHECK (
            coalesce(stage, '') !~* {_SHOWMATCH} AND coalesce(series, '') !~* {_SHOWMATCH}
        );
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE matches DROP CONSTRAINT matches_not_showmatch;")
