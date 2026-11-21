"""Key players on the vlr.gg player ID; add the rosters used to resolve it

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-21

vlrggapi's match detail names players but doesn't give their vlr.gg IDs, so the pipeline
resolves each (team, handle) against rosters that do carry IDs:
  event_rosters        from /v2/event/{id}: the players each team registered for an event
  team_roster_players  from /v2/team/{id}/roster: active, benched and former players and
                       everyone in the team's transfer log (covers stand-ins)

players.id is now the vlr.gg player ID, and handle is just the latest one seen, so a
renamed player stays one row. map_players.handle keeps the handle used in each match.

Player stats keyed the old way can't be converted (they have no vlr.gg ID), so this
clears map-level data and marks every match for re-parsing; the next `sync` refetches
and rebuilds it. raw_responses, events, teams and matches are kept.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# player_agent_maps depends on map_players.player_id, so it is rebuilt around the change.
_PLAYER_AGENT_MAPS_V2 = """
CREATE VIEW player_agent_maps AS
SELECT
    pl.map_id,
    mp.match_id,
    m.event_id,
    mp.map_name,
    mp.patch,
    m.played_at,
    pl.player_id,
    p.handle,                      -- latest handle
    pl.handle AS match_handle,     -- handle used in this match
    pl.team_id,
    pl.agent,
    a.role,
    tm.won,
    mp.team1_rounds + mp.team2_rounds AS rounds_played,
    pl.rating, pl.acs, pl.kills, pl.deaths, pl.assists, pl.kast, pl.adr,
    pl.hs_pct, pl.first_kills, pl.first_deaths
FROM map_players pl
JOIN players p ON p.id = pl.player_id
JOIN maps mp ON mp.id = pl.map_id
JOIN matches m ON m.id = mp.match_id
LEFT JOIN team_maps tm ON tm.map_id = pl.map_id AND tm.team_id = pl.team_id
LEFT JOIN agents a ON lower(a.name) = lower(pl.agent);
"""

_PLAYER_AGENT_MAPS_V1 = """
CREATE VIEW player_agent_maps AS
SELECT
    pl.map_id,
    mp.match_id,
    m.event_id,
    mp.map_name,
    mp.patch,
    m.played_at,
    pl.player_id,
    p.handle,
    pl.team_id,
    pl.agent,
    a.role,
    tm.won,
    mp.team1_rounds + mp.team2_rounds AS rounds_played,
    pl.rating, pl.acs, pl.kills, pl.deaths, pl.assists, pl.kast, pl.adr,
    pl.hs_pct, pl.first_kills, pl.first_deaths
FROM map_players pl
JOIN players p ON p.id = pl.player_id
JOIN maps mp ON mp.id = pl.map_id
JOIN matches m ON m.id = mp.match_id
LEFT JOIN team_maps tm ON tm.map_id = pl.map_id AND tm.team_id = pl.team_id
LEFT JOIN agents a ON lower(a.name) = lower(pl.agent);
"""


def upgrade() -> None:
    op.execute(
        """
        DROP VIEW player_agent_maps;

        -- Map-level rows are rebuilt by the next sync (see module docstring)
        DELETE FROM maps;            -- cascades to team_maps, map_players, rounds
        DELETE FROM veto_steps;
        UPDATE matches SET parsed_at = NULL;
        DROP TABLE map_players;
        DROP TABLE players;

        CREATE TABLE players (
            id           integer     PRIMARY KEY,          -- vlr.gg player ID
            handle       text        NOT NULL,             -- latest handle seen
            last_seen_at timestamptz,                      -- match time `handle` is from
            updated_at   timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX players_handle_lower_idx ON players (lower(handle));

        CREATE TABLE map_players (
            map_id       bigint       NOT NULL REFERENCES maps (id) ON DELETE CASCADE,
            player_id    integer      NOT NULL REFERENCES players (id),
            team_id      integer      NOT NULL REFERENCES teams (id),
            handle       text         NOT NULL,            -- as shown in this match
            agent        text         NOT NULL,
            rating       numeric(4,2),
            acs          smallint,
            kills        smallint,
            deaths       smallint,
            assists      smallint,
            kast         numeric(5,2),
            adr          numeric(5,1),
            hs_pct       numeric(5,2),
            first_kills  smallint,
            first_deaths smallint,
            PRIMARY KEY (map_id, player_id)
        );
        CREATE INDEX map_players_player_idx ON map_players (player_id);
        CREATE INDEX map_players_agent_idx ON map_players (agent);

        CREATE TABLE event_rosters (
            event_id   integer     NOT NULL REFERENCES events (id) ON DELETE CASCADE,
            team_id    integer     NOT NULL REFERENCES teams (id),
            player_id  integer     NOT NULL,               -- vlr.gg player ID
            handle     text        NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),
            -- One row per handle seen, so matches stored under a player's old handle
            -- still resolve after they rename
            PRIMARY KEY (event_id, team_id, player_id, handle)
        );
        CREATE INDEX event_rosters_lookup_idx
            ON event_rosters (event_id, team_id, lower(handle));

        CREATE TABLE team_roster_players (
            team_id    integer     NOT NULL REFERENCES teams (id),
            player_id  integer     NOT NULL,               -- vlr.gg player ID
            handle     text        NOT NULL,
            status     text        NOT NULL
                       CHECK (status IN ('active', 'benched', 'former', 'transaction')),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (team_id, player_id, handle)
        );
        CREATE INDEX team_roster_players_lookup_idx
            ON team_roster_players (team_id, lower(handle));
        """
    )
    op.execute(_PLAYER_AGENT_MAPS_V2)


def downgrade() -> None:
    op.execute(
        """
        DROP VIEW player_agent_maps;
        DROP TABLE team_roster_players, event_rosters;

        DELETE FROM maps;
        DELETE FROM veto_steps;
        UPDATE matches SET parsed_at = NULL;
        DROP TABLE map_players;
        DROP TABLE players;

        CREATE TABLE players (
            id         bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            vlr_id     integer     UNIQUE,
            handle     text        NOT NULL UNIQUE,
            updated_at timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE map_players (
            map_id       bigint       NOT NULL REFERENCES maps (id) ON DELETE CASCADE,
            player_id    bigint       NOT NULL REFERENCES players (id),
            team_id      integer      NOT NULL REFERENCES teams (id),
            agent        text         NOT NULL,
            rating       numeric(4,2),
            acs          smallint,
            kills        smallint,
            deaths       smallint,
            assists      smallint,
            kast         numeric(5,2),
            adr          numeric(5,1),
            hs_pct       numeric(5,2),
            first_kills  smallint,
            first_deaths smallint,
            PRIMARY KEY (map_id, player_id)
        );
        CREATE INDEX map_players_player_idx ON map_players (player_id);
        CREATE INDEX map_players_agent_idx ON map_players (agent);
        """
    )
    op.execute(_PLAYER_AGENT_MAPS_V1)
