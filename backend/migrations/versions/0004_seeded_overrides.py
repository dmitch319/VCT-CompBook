"""Hand-maintained player-ID overrides and match exclusions

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-22

Loaded from seed/ by `vct-pipeline seed`, like agents.

player_overrides  (team, handle) -> vlr.gg player ID, checked before any roster. For
                  players no roster can resolve, e.g. a one-event stand-in who never
                  appears in the team's transfer log.
match_exclusions  Completed matches to leave out, with the reason, e.g. a match whose
                  lineup is a full emergency stand-in roster. sync stops retrying them
                  and status counts them as excluded, not pending.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE player_overrides (
            team_id   integer NOT NULL,
            handle    text    NOT NULL,     -- as shown in match details
            player_id integer NOT NULL,     -- vlr.gg player ID
            note      text,
            PRIMARY KEY (team_id, handle)
        );

        CREATE TABLE match_exclusions (
            match_id integer PRIMARY KEY,   -- vlr.gg match ID (no FK: may not be listed yet)
            reason   text    NOT NULL
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE match_exclusions, player_overrides;")
