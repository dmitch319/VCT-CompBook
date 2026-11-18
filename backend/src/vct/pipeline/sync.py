"""Fetch completed matches we don't have yet for every tracked event.

Completed matches are immutable, so each is fetched once: a match is "missing" until
its detail has been parsed (matches.parsed_at). A second run with nothing new
makes only one list request per tracked event.
"""

import logging

from vct.db import Connection
from vct.pipeline.client import VlrClient
from vct.pipeline.parse import parse_event_matches, parse_match_detail
from vct.pipeline.runs import Run
from vct.pipeline.validate import validate_match

log = logging.getLogger(__name__)


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
    try:
        for event_id in event_ids if event_ids is not None else tracked_event_ids(conn):
            resp = client.get("/v2/events/matches", event_id=str(event_id))
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

            for match_id in todo:
                if limit is not None and run.matches_fetched + run.matches_failed >= limit:
                    break
                sync_match(conn, client, run, match_id)
            if limit is not None and run.matches_fetched + run.matches_failed >= limit:
                log.info("reached --limit %d", limit)
                break
    except Exception as exc:
        run.finish(exc)
        raise
    run.finish()
    return run


def sync_match(conn: Connection, client: VlrClient, run: Run, match_id: int) -> None:
    """Fetch, store, parse and validate one match. A failure is recorded on the run and
    doesn't stop the others; the raw response is kept either way."""
    try:
        resp = client.get("/v2/match/details", match_id=str(match_id))
        with conn.transaction():
            parse_match_detail(conn, match_id, resp.body, raw_id=resp.raw_id)
            issues = validate_match(conn, match_id)
    except Exception as exc:
        log.warning("match %d failed: %s", match_id, exc)
        run.fail_match(match_id, exc)
        return
    run.matches_fetched += 1
    run.warn(match_id, issues)
    if issues:
        log.warning("match %d: %s", match_id, "; ".join(issues))
