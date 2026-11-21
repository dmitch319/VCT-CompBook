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


def reparse(conn: Connection) -> Run:
    run = Run(conn, "reparse")
    try:
        # Oldest first, so the newest listing of an event wins
        for row in sorted(latest_responses(conn, "/v2/events"), key=lambda r: r["id"]):
            with conn.transaction():
                run.events_checked += parse_events(conn, row["body"])

        for row in latest_responses(conn, "/v2/events/matches"):
            event_id = to_int(row["params"].get("event_id"))
            if event_id is None:
                continue
            with conn.transaction():
                _ensure_event(conn, event_id)
                parse_event_matches(conn, event_id, row["body"])

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
            if match_id is None:
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
    except Exception as exc:
        run.finish(exc)
        raise
    run.finish()
    return run
