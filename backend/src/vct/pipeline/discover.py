"""Find VCT events on vlr.gg and mark the tier-1 ones as tracked."""

import logging

from vct.db import Connection
from vct.pipeline.client import VlrClient
from vct.pipeline.parse import parse_events
from vct.pipeline.runs import Run

log = logging.getLogger(__name__)


def discover(conn: Connection, client: VlrClient, completed_pages: int = 3) -> Run:
    """Upcoming/ongoing events, plus the first `completed_pages` pages of completed ones.
    Use a larger `completed_pages` once for a backfill of past seasons."""
    run = Run(conn, "discover")
    try:
        requests: list[dict[str, str | int]] = [{"q": "upcoming"}]
        requests += [{"q": "completed", "page": p} for p in range(1, completed_pages + 1)]
        for params in requests:
            resp = client.get("/v2/events", **params)
            with conn.transaction():
                n = parse_events(conn, resp.body)
            run.events_checked += n
            log.info("events %s: %d listed", params, n)
    except Exception as exc:
        run.finish(exc)
        raise
    run.finish()
    return run


def set_tracked(conn: Connection, event_id: int, tracked: bool = True) -> None:
    """Track (or stop tracking) an event by hand. Creates a placeholder row if the event
    hasn't been discovered yet; the next `discover` fills in its name."""
    conn.execute(
        """
        INSERT INTO events (id, name, tracked) VALUES (%s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET tracked = EXCLUDED.tracked, updated_at = now()
        """,
        (event_id, f"event {event_id}", tracked),
    )
