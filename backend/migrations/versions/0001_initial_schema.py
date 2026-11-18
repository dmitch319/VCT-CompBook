"""Initial Postgres schema: raw, normalized and analytics layers

Revision ID: 0001
Revises:
Create Date: 2026-11-17

Layers (see docs/Design Document.docx, "Data Model"):
  raw         raw_responses, sync_runs
  normalized  agents, events, teams, players, matches, maps, team_maps, map_players,
              rounds, veto_steps (keyed on vlr.gg IDs where vlrggapi exposes them)
  analytics   team_map_comps, player_agent_maps (views)
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        -- ------------------------------------------------------------------ raw layer

        -- Every vlrggapi response, stored as received, so every table below can be
        -- rebuilt (`vct-pipeline reparse`) without fetching from vlr.gg again.
        CREATE TABLE raw_responses (
            id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            endpoint    text        NOT NULL,                -- e.g. '/v2/match/details'
            params      jsonb       NOT NULL DEFAULT '{}',   -- query params sent
            status_code smallint    NOT NULL,
            body        jsonb,                               -- parsed JSON body
            body_raw    bytea,                               -- only if the body wasn't JSON
            fetched_at  timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT raw_responses_one_body CHECK (body IS NULL OR body_raw IS NULL)
        );
        CREATE INDEX raw_responses_lookup_idx
            ON raw_responses (endpoint, params, fetched_at DESC);

        CREATE TABLE sync_runs (
            id              bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            command         text        NOT NULL,            -- discover / sync / reparse
            started_at      timestamptz NOT NULL DEFAULT now(),
            finished_at     timestamptz,
            status          text        NOT NULL DEFAULT 'running'
                            CHECK (status IN ('running', 'success', 'partial', 'failed')),
            events_checked  integer     NOT NULL DEFAULT 0,
            matches_fetched integer     NOT NULL DEFAULT 0,
            matches_failed  integer     NOT NULL DEFAULT 0,
            error           text,
            details         jsonb       NOT NULL DEFAULT '{}' -- per-match errors, warnings
        );

        -- ----------------------------------------------------------- normalized layer

        CREATE TABLE agents (
            name text PRIMARY KEY,
            role text NOT NULL CHECK (role IN ('Duelist', 'Initiator', 'Controller', 'Sentinel'))
        );
        CREATE UNIQUE INDEX agents_name_lower_idx ON agents (lower(name));

        CREATE TABLE events (
            id          integer     PRIMARY KEY,             -- vlr.gg event ID
            name        text        NOT NULL,
            region      text,
            status      text,                                -- upcoming / ongoing / completed
            dates_label text,                                -- as shown on vlr.gg, e.g. 'Jun 7—22'
            start_date  date,
            end_date    date,
            tracked     boolean     NOT NULL DEFAULT false,  -- followed by `sync`
            updated_at  timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX events_tracked_idx ON events (id) WHERE tracked;

        CREATE TABLE teams (
            id         integer     PRIMARY KEY,              -- vlr.gg team ID
            name       text        NOT NULL,
            tag        text,
            updated_at timestamptz NOT NULL DEFAULT now()
        );

        -- vlrggapi's match detail gives player handles but not vlr.gg player IDs, so for
        -- now a player is identified by handle. vlr_id is filled in once we can resolve it.
        CREATE TABLE players (
            id         bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            vlr_id     integer     UNIQUE,
            handle     text        NOT NULL UNIQUE,
            updated_at timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE matches (
            id             integer     PRIMARY KEY,          -- vlr.gg match ID
            event_id       integer     NOT NULL REFERENCES events (id),
            stage          text,                             -- e.g. 'Playoffs'
            series         text,                             -- e.g. 'Upper Final'
            played_at      timestamptz,
            status         text,                             -- as listed, e.g. 'Completed'
            best_of        smallint    CHECK (best_of IN (1, 3, 5)),
            team1_id       integer     REFERENCES teams (id),
            team2_id       integer     REFERENCES teams (id),
            team1_score    smallint,
            team2_score    smallint,
            winner_team_id integer     REFERENCES teams (id),
            patch          text,
            veto_text      text,                             -- veto line as shown on vlr.gg
            detail_raw_id  bigint      REFERENCES raw_responses (id),
            parsed_at      timestamptz,                      -- NULL until details are parsed
            updated_at     timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX matches_event_idx ON matches (event_id);
        CREATE INDEX matches_played_at_idx ON matches (played_at DESC);

        CREATE TABLE maps (
            id                bigint   GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            match_id          integer  NOT NULL REFERENCES matches (id) ON DELETE CASCADE,
            map_order         smallint NOT NULL CHECK (map_order BETWEEN 1 AND 5),
            vlr_game_id       integer  UNIQUE,               -- vlr.gg game ID, when known
            map_name          text     NOT NULL,
            patch             text,
            picked_by_team_id integer  REFERENCES teams (id), -- NULL for the decider
            duration_seconds  integer,
            team1_rounds      smallint NOT NULL,
            team2_rounds      smallint NOT NULL,
            winner_team_id    integer  REFERENCES teams (id),
            UNIQUE (match_id, map_order)
        );
        CREATE INDEX maps_map_name_idx ON maps (map_name);

        CREATE TABLE team_maps (
            map_id             bigint   NOT NULL REFERENCES maps (id) ON DELETE CASCADE,
            team_id            integer  NOT NULL REFERENCES teams (id),
            rounds_won         smallint NOT NULL,
            rounds_won_attack  smallint,
            rounds_won_defense smallint,
            rounds_won_ot      smallint,
            start_side         text     CHECK (start_side IN ('attack', 'defense')),
            won                boolean  NOT NULL,
            comp_key           text,     -- the five agents, sorted, comma-joined
            archetype          text,     -- e.g. '1D-2I-1C-1S'
            PRIMARY KEY (map_id, team_id)
        );
        CREATE INDEX team_maps_comp_idx ON team_maps (comp_key);
        CREATE INDEX team_maps_archetype_idx ON team_maps (archetype);

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
            kast         numeric(5,2),                       -- percent, 0-100
            adr          numeric(5,1),
            hs_pct       numeric(5,2),                       -- percent, 0-100
            first_kills  smallint,
            first_deaths smallint,
            PRIMARY KEY (map_id, player_id)
        );
        CREATE INDEX map_players_player_idx ON map_players (player_id);
        CREATE INDEX map_players_agent_idx ON map_players (agent);

        CREATE TABLE rounds (
            map_id         bigint   NOT NULL REFERENCES maps (id) ON DELETE CASCADE,
            round_number   smallint NOT NULL CHECK (round_number >= 1),
            winner_team_id integer  REFERENCES teams (id),
            winner_side    text     CHECK (winner_side IN ('attack', 'defense')),
            win_type       text,                             -- elim / boom / defuse / time
            PRIMARY KEY (map_id, round_number)
        );

        CREATE TABLE veto_steps (
            match_id   integer  NOT NULL REFERENCES matches (id) ON DELETE CASCADE,
            step_order smallint NOT NULL CHECK (step_order >= 1),
            team_id    integer  REFERENCES teams (id),       -- NULL for the decider
            action     text     NOT NULL CHECK (action IN ('ban', 'pick', 'decider')),
            map_name   text     NOT NULL,
            PRIMARY KEY (match_id, step_order)
        );

        -- ------------------------------------------------------------ analytics layer

        CREATE VIEW team_map_comps AS
        SELECT
            tm.map_id,
            mp.match_id,
            m.event_id,
            mp.map_name,
            mp.patch,
            m.played_at,
            tm.team_id,
            CASE WHEN tm.team_id = m.team1_id THEN m.team2_id ELSE m.team1_id END
                AS opponent_team_id,
            tm.comp_key,
            tm.archetype,
            tm.won,
            tm.rounds_won,
            tm.rounds_won_attack,
            tm.rounds_won_defense,
            tm.start_side
        FROM team_maps tm
        JOIN maps mp ON mp.id = tm.map_id
        JOIN matches m ON m.id = mp.match_id;

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
    )


def downgrade() -> None:
    op.execute(
        """
        DROP VIEW IF EXISTS player_agent_maps;
        DROP VIEW IF EXISTS team_map_comps;
        DROP TABLE IF EXISTS veto_steps, rounds, map_players, team_maps, maps, matches,
            players, teams, events, agents, sync_runs, raw_responses;
        """
    )
