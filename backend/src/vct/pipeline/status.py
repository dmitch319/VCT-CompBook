"""Database health summary for `vct-pipeline status`.

Scoped to tracked events, since those are what the site analyses. Showmatches are never
stored (migration 0003); showmatches_skipped is how many the last sync's match lists held.
matches_excluded are completed matches left out by hand (seed/match_exclusions.csv), so
matches_pending is what a sync would still try to fetch.

comp_coverage_pct  Share of team-map rows (one per team per played map) with a complete
                   comp: exactly five agents, all with a known role. This is the share of
                   maps the comp and archetype analysis can use.
duplicate_handles  Handles (case-insensitive) held by more than one vlr.gg player ID.
                   Different people can share a handle, so this is a list to eyeball;
                   duplicate_handles_same_team, the same handle under two IDs on one
                   team, almost certainly means one person stored twice.
"""

from typing import Any

from vct.db import Connection

_QUERIES: dict[str, str] = {
    "events_tracked": "SELECT count(*) FROM events WHERE tracked",
    "matches_completed": """
        SELECT count(*) FROM matches m JOIN events e ON e.id = m.event_id
        WHERE e.tracked AND lower(m.status) = 'completed'
    """,
    "matches_excluded": """
        SELECT count(*) FROM matches m JOIN events e ON e.id = m.event_id
        WHERE e.tracked AND m.id IN (SELECT match_id FROM match_exclusions)
    """,
    "matches_parsed": """
        SELECT count(*) FROM matches m JOIN events e ON e.id = m.event_id
        WHERE e.tracked AND m.parsed_at IS NOT NULL
    """,
    "matches_pending": """
        SELECT count(*) FROM matches m JOIN events e ON e.id = m.event_id
        WHERE e.tracked AND lower(m.status) = 'completed' AND m.parsed_at IS NULL
          AND m.id NOT IN (SELECT match_id FROM match_exclusions)
    """,
    "matches_missing_played_at": """
        SELECT count(*) FROM matches m JOIN events e ON e.id = m.event_id
        WHERE e.tracked AND m.parsed_at IS NOT NULL AND m.played_at IS NULL
    """,
    "maps": """
        SELECT count(*) FROM maps mp JOIN matches m ON m.id = mp.match_id
        JOIN events e ON e.id = m.event_id WHERE e.tracked
    """,
    "team_maps": """
        SELECT count(*) FROM team_map_comps c JOIN events e ON e.id = c.event_id
        WHERE e.tracked
    """,
    "team_maps_full_comp": """
        SELECT count(*) FROM team_map_comps c JOIN events e ON e.id = c.event_id
        WHERE e.tracked
          AND array_length(string_to_array(c.comp_key, ','), 1) = 5
          AND c.archetype NOT LIKE '%%?%%'
    """,
    "players": "SELECT count(*) FROM players",
    "duplicate_handles": """
        SELECT count(*) FROM (
            SELECT lower(handle) FROM players GROUP BY 1 HAVING count(*) > 1
        ) d
    """,
    "duplicate_handles_same_team": """
        SELECT count(*) FROM (
            SELECT team_id, lower(handle) FROM map_players
            GROUP BY 1, 2 HAVING count(DISTINCT player_id) > 1
        ) d
    """,
    "players_without_stats": """
        SELECT count(*) FROM players p
        WHERE NOT EXISTS (SELECT 1 FROM map_players pl WHERE pl.player_id = p.id)
    """,
}


def collect_status(conn: Connection) -> dict[str, Any]:
    status: dict[str, Any] = {}
    for key, sql in _QUERIES.items():
        row = conn.execute(f"SELECT ({sql}) AS n").fetchone()
        status[key] = int(row["n"]) if row else 0

    team_maps = status["team_maps"]
    status["comp_coverage_pct"] = (
        round(100 * status["team_maps_full_comp"] / team_maps, 2) if team_maps else None
    )

    last = conn.execute(
        """
        SELECT id, command, status, started_at, finished_at, matches_fetched, matches_failed,
               details
        FROM sync_runs WHERE command = 'sync' ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    status["showmatches_skipped"] = (
        int(last["details"].get("showmatches_skipped", 0)) if last is not None else None
    )
    status["last_sync"] = (
        None
        if last is None
        else {
            "run": last["id"],
            "status": last["status"],
            "finished_at": last["finished_at"].isoformat() if last["finished_at"] else None,
            "matches_fetched": last["matches_fetched"],
            "matches_failed": last["matches_failed"],
        }
    )
    return status


def incomplete_comps(conn: Connection, limit: int = 20) -> list[dict[str, Any]]:
    """The tracked team-map rows that don't count toward comp coverage, for diagnosis."""
    return conn.execute(
        """
        SELECT c.match_id, c.map_name, c.team_id, c.comp_key, c.archetype
        FROM team_map_comps c JOIN events e ON e.id = c.event_id
        WHERE e.tracked
          AND (c.comp_key IS NULL
               OR array_length(string_to_array(c.comp_key, ','), 1) <> 5
               OR c.archetype LIKE '%%?%%')
        ORDER BY c.match_id DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
