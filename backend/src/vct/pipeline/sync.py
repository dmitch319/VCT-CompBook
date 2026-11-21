"""Fetch completed matches we don't have yet for every tracked event.

Completed matches are immutable, so each is fetched once: a match is "missing" until
its detail has been parsed (matches.parsed_at). A second run with nothing new
makes only one list request per tracked event.

Players are stored under their vlr.gg ID, which match details don't include, so rosters
are fetched to resolve handles (see rosters.py): the event roster once per event that has
new matches, and a team's roster history only when a handle isn't on the event roster,
at most once per team per run. A match with a player that still can't be resolved is not
stored; it is recorded as failed and retried by the next sync.
"""

import logging
from dataclasses import dataclass, field

from vct.db import Connection
from vct.pipeline.client import VlrClient
from vct.pipeline.parse import parse_event_matches, parse_match_detail
from vct.pipeline.rosters import (
    parse_event_roster,
    parse_team_roster,
    resolve_match,
    try_resolve_match,
)
from vct.pipeline.runs import Run
from vct.pipeline.validate import validate_match

log = logging.getLogger(__name__)


@dataclass
class SyncState:
    run: Run
    team_rosters_fetched: set[int] = field(default_factory=set)


def tracked_event_ids(conn: Connection) -> list[int]:
    rows = conn.execute("SELECT id FROM events WHERE tracked ORDER BY id").fetchall()
    return [int(r["id"]) for r in rows]


def sync(
    conn: Connection,
    client: VlrClient,
    event_ids: list[int] | None = None,
    limit: int | None = None,
) -> Run:
    """Sync the given events (default: all tracked). `limit` caps match-detail fetches
    for this run, which is handy for a first test against a live vlrggapi."""
    run = Run(conn, "sync")
    state = SyncState(run)

    def at_limit() -> bool:
        return limit is not None and run.matches_fetched + run.matches_failed >= limit

    try:
        for event_id in event_ids if event_ids is not None else tracked_event_ids(conn):
            resp = client.get("/v2/events/matches", event_id=event_id)
            with conn.transaction():
                listed = parse_event_matches(conn, event_id, resp.body)
            run.events_checked += 1

            completed = [m.match_id for m in listed if m.completed]
            have = {
                int(r["id"])
                for r in conn.execute(
                    "SELECT id FROM matches WHERE id = ANY(%s) AND parsed_at IS NOT NULL",
                    (completed,),
                ).fetchall()
            }
            todo = [mid for mid in completed if mid not in have]
            log.info("event %d: %d completed, %d new", event_id, len(completed), len(todo))
            if not todo or at_limit():
                continue

            # Refreshed every run that has new matches: stand-ins get added mid-event
            roster = client.get("/v2/event/{event_id}", event_id=event_id, include="teams")
            with conn.transaction():
                parse_event_roster(conn, event_id, roster.body)

            for match_id in todo:
                if at_limit():
                    break
                sync_match(conn, client, state, match_id)
            if at_limit():
                log.info("reached --limit %d", limit)
                break
    except Exception as exc:
        run.finish(exc)
        raise
    run.finish()
    return run


def sync_match(conn: Connection, client: VlrClient, state: SyncState, match_id: int) -> None:
    """Fetch, store, resolve, parse and validate one match. A failure is recorded on the
    run and doesn't stop the others; raw responses are kept either way."""
    run = state.run
    try:
        resp = client.get("/v2/match/details", match_id=match_id)

        _, missing = try_resolve_match(conn, match_id, resp.body)
        for team_id in sorted(missing.keys() - state.team_rosters_fetched):
            state.team_rosters_fetched.add(team_id)
            roster = client.get("/v2/team/{team_id}/roster", team_id=team_id)
            with conn.transaction():
                parse_team_roster(conn, team_id, roster.body)

        with conn.transaction():
            player_ids = resolve_match(conn, match_id, resp.body)
            parse_match_detail(conn, match_id, resp.body, player_ids, raw_id=resp.raw_id)
            issues = validate_match(conn, match_id)
    except Exception as exc:
        log.warning("match %d failed: %s", match_id, exc)
        run.fail_match(match_id, exc)
        return
    run.matches_fetched += 1
    run.warn(match_id, issues)
    if issues:
        log.warning("match %d: %s", match_id, "; ".join(issues))
