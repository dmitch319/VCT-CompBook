"""Rebuild the normalized tables from raw_responses, with no requests to vlr.gg.

Replays stored responses in dependency order:
  event lists -> each event's match list -> rosters -> each match's detail
Lists and details use the latest successful response per request. Rosters replay every
stored response, oldest first, because roster rows accumulate (a stand-in who has since
left a roster must still resolve). Run it after fixing a parser bug or adding a stat.
"""

import logging
from typing import Any

from vct.db import Connection
from vct.pipeline.parse import parse_event_matches, parse_events, parse_match_detail, to_int
from vct.pipeline.rosters import parse_event_roster, parse_team_roster, resolve_match
from vct.pipeline.runs import Run
from vct.pipeline.validate import validate_match

log = logging.getLogger(__name__)


def latest_responses(conn: Connection, endpoint: str) -> list[dict[str, Any]]:
    """The newest successful stored response per distinct set of params."""
    return conn.execute(
        """
        SELECT DISTINCT ON (params) id, params, body
        FROM raw_responses
        WHERE endpoint = %s AND status_code = 200 AND body IS NOT NULL
        ORDER BY params, fetched_at DESC, id DESC
        """,
        (endpoint,),
    ).fetchall()


def all_responses(conn: Connection, endpoint: str) -> list[dict[str, Any]]:
    """Every successful stored response, oldest first."""
    return conn.execute(
        """
        SELECT id, params, body
        FROM raw_responses
        WHERE endpoint = %s AND status_code = 200 AND body IS NOT NULL
        ORDER BY fetched_at, id
        """,
        (endpoint,),
    ).fetchall()


def _ensure_event(conn: Connection, event_id: int) -> None:
    """The event may only be known from a hand-tracked placeholder."""
    conn.execute(
        "INSERT INTO events (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        (event_id, f"event {event_id}"),
    )


def refresh_players(conn: Connection) -> int:
    """Recompute each player's current handle and last_seen_at from their stored matches
    (latest played_at wins, then highest match ID).

    The per-match upsert only moves last_seen_at forward, so a value that was once too
    late (e.g. from a mis-dated match since corrected) would otherwise never heal, and a
    rebuild from scratch would disagree with an incrementally built database."""
    return conn.execute(
        """
        UPDATE players p
        SET handle = x.handle, last_seen_at = x.played_at, updated_at = now()
        FROM (
            SELECT DISTINCT ON (pl.player_id) pl.player_id, pl.handle, m.played_at
            FROM map_players pl
            JOIN maps mp ON mp.id = pl.map_id
            JOIN matches m ON m.id = mp.match_id
            ORDER BY pl.player_id, m.played_at DESC NULLS LAST, m.id DESC
        ) x
        WHERE p.id = x.player_id
          AND (p.handle, p.last_seen_at) IS DISTINCT FROM (x.handle, x.played_at)
        """
    ).rowcount


def reparse(conn: Connection) -> Run:
    run = Run(conn, "reparse")
    try:
        # Oldest first, so the newest listing of an event wins
        for row in sorted(latest_responses(conn, "/v2/events"), key=lambda r: r["id"]):
            with conn.transaction():
                run.events_checked += parse_events(conn, row["body"])

        # Showmatches (dropped from the match lists) and hand-excluded matches are skipped
        skip = {
            int(r["match_id"])
            for r in conn.execute("SELECT match_id FROM match_exclusions").fetchall()
        }
        for row in latest_responses(conn, "/v2/events/matches"):
            event_id = to_int(row["params"].get("event_id"))
            if event_id is None:
                continue
            with conn.transaction():
                _ensure_event(conn, event_id)
                listed = parse_event_matches(conn, event_id, row["body"])
            skip.update(m.match_id for m in listed if m.showmatch)

        for row in all_responses(conn, "/v2/event/{event_id}"):
            event_id = to_int(row["params"].get("event_id"))
            if event_id is None:
                continue
            with conn.transaction():
                _ensure_event(conn, event_id)
                parse_event_roster(conn, event_id, row["body"])

        for row in all_responses(conn, "/v2/team/{team_id}/roster"):
            team_id = to_int(row["params"].get("team_id"))
            if team_id is not None:
                with conn.transaction():
                    parse_team_roster(conn, team_id, row["body"])

        for row in latest_responses(conn, "/v2/match/details"):
            match_id = to_int(row["params"].get("match_id"))
            if match_id is None or match_id in skip:
                continue
            try:
                with conn.transaction():
                    player_ids = resolve_match(conn, match_id, row["body"])
                    parse_match_detail(conn, match_id, row["body"], player_ids, raw_id=row["id"])
                    issues = validate_match(conn, match_id)
            except Exception as exc:
                log.warning("match %d failed: %s", match_id, exc)
                run.fail_match(match_id, exc)
                continue
            run.matches_fetched += 1
            run.warn(match_id, issues)

        with conn.transaction():
            refresh_players(conn)
    except Exception as exc:
        run.finish(exc)
        raise
    run.finish()
    return run
