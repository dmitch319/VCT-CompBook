"""vct-pipeline: vlrggapi -> Postgres.

  vct-pipeline ping                     check vlrggapi is reachable
  vct-pipeline seed                     load seed/agent_roles.csv
  vct-pipeline discover [--pages N]     find VCT events, mark tier-1 ones tracked
  vct-pipeline track EVENT_ID [--off]   track (or untrack) an event by hand
  vct-pipeline sync [--event ID] [--limit N]
                                        fetch new completed matches for tracked events
  vct-pipeline reparse                  rebuild tables from raw_responses (no fetching)

Reads DATABASE_URL and VLRGGAPI_URL from the environment or the repo's .env.
"""

import argparse
import logging
import sys

from vct.config import ConfigError, get_settings
from vct.db import connect
from vct.pipeline.client import VlrClient
from vct.pipeline.discover import discover, set_tracked
from vct.pipeline.reparse import reparse
from vct.pipeline.runs import Run
from vct.pipeline.seed import load_agents
from vct.pipeline.sync import sync


def _report(run: Run) -> int:
    print(
        f"run {run.id} ({run.command}): {run.events_checked} events, "
        f"{run.matches_fetched} matches parsed, {run.matches_failed} failed, "
        f"{len(run.warnings)} with warnings"
    )
    for match_id, err in run.errors.items():
        print(f"  match {match_id}: {err}", file=sys.stderr)
    return 1 if run.matches_failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vct-pipeline", description="vlrggapi -> Postgres")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ping", help="check vlrggapi is reachable")
    sub.add_parser("seed", help="load seed/agent_roles.csv into agents")
    p = sub.add_parser("discover", help="find VCT events and mark tier-1 ones tracked")
    p.add_argument("--pages", type=int, default=3, help="pages of completed events (default 3)")
    p = sub.add_parser("track", help="track or untrack an event by vlr.gg ID")
    p.add_argument("event_id", type=int)
    p.add_argument("--off", action="store_true", help="stop tracking")
    p = sub.add_parser("sync", help="fetch new completed matches for tracked events")
    p.add_argument("--event", type=int, action="append", help="only this event (repeatable)")
    p.add_argument("--limit", type=int, help="fetch at most N match details")
    sub.add_parser("reparse", help="rebuild normalized tables from raw_responses")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    with connect(autocommit=True) as conn:
        if args.command == "seed":
            print(f"loaded {load_agents(conn)} agents")
            return 0
        if args.command == "track":
            set_tracked(conn, args.event_id, not args.off)
            print(f"event {args.event_id}: tracked={not args.off}")
            return 0
        if args.command == "reparse":
            return _report(reparse(conn))

        with VlrClient(conn, settings.vlrggapi_url) as client:
            if args.command == "ping":
                ok = client.ping()
                print(f"vlrggapi {settings.vlrggapi_url}: {'up' if ok else 'unreachable'}")
                return 0 if ok else 1
            if args.command == "discover":
                return _report(discover(conn, client, completed_pages=args.pages))
            if args.command == "sync":
                return _report(sync(conn, client, event_ids=args.event, limit=args.limit))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
