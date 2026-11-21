"""Resolve player handles to vlr.gg player IDs.

vlrggapi's match detail names each player but doesn't give their vlr.gg ID. Rosters do,
so a player in a match is looked up by (team, handle), case-insensitively, in order:

  1. the team's roster for the match's event        (event_rosters, /v2/event/{id})
  2. the team's roster and transfer history          (team_roster_players,
                                                      /v2/team/{id}/roster)
  3. the same team's earlier matches                 (map_players)

A handle that matches no ID, or two different IDs, is unresolved, and the match is not
stored until it resolves (see sync.py). Roster rows only accumulate, one per
(player, handle) seen, never deleted or overwritten: a stand-in who has since left a
roster, or a match stored under a player's old handle, still resolves.
"""

from collections import defaultdict
from typing import Any

from vct.db import Connection
from vct.pipeline.parse import ParseError, segments, to_int


class UnresolvedPlayers(ParseError):
    def __init__(self, match_id: int, missing: dict[int, set[str]]) -> None:
        listed = "; ".join(
            f"team {team_id}: {', '.join(sorted(handles))}"
            for team_id, handles in sorted(missing.items())
        )
        super().__init__(f"match {match_id}: no vlr.gg player ID for {listed}")
        self.missing = missing


def _upsert_team(conn: Connection, team_id: int, name: str) -> None:
    conn.execute(
        """
        INSERT INTO teams (id, name) VALUES (%s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (team_id, name or f"team {team_id}"),
    )


def parse_event_roster(conn: Connection, event_id: int, body: Any) -> int:
    """Upsert event_rosters from /v2/event/{id}, whose data.segments is an object with a
    `teams` list: [{id, name, players: [{id, name}]}]. Returns players upserted."""
    data = body.get("data") if isinstance(body, dict) else None
    seg = data.get("segments") if isinstance(data, dict) else None
    teams = seg.get("teams") if isinstance(seg, dict) else None
    count = 0
    for team in teams if isinstance(teams, list) else []:
        team_id = to_int(team.get("id"))
        if team_id is None:
            continue
        _upsert_team(conn, team_id, (team.get("name") or "").strip())
        for p in team.get("players") or []:
            player_id, handle = to_int(p.get("id")), (p.get("name") or "").strip()
            if player_id is None or not handle:
                continue
            conn.execute(
                """
                INSERT INTO event_rosters (event_id, team_id, player_id, handle)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (event_id, team_id, player_id, handle) DO UPDATE SET
                    updated_at = now()
                """,
                (event_id, team_id, player_id, handle),
            )
            count += 1
    return count


def parse_team_roster(conn: Connection, team_id: int, body: Any) -> int:
    """Upsert team_roster_players from /v2/team/{id}/roster: one segment with active,
    benched and former lists ({id, alias}) and a transactions list ({player: {id, name}}).
    Staff are skipped. A player listed twice keeps the most current status."""
    segs = segments(body)
    if not segs:
        return 0
    seg = segs[0]
    entries: list[tuple[int, str, str]] = []
    for status in ("active", "benched", "former"):
        for p in seg.get(status) or []:
            entries.append((to_int(p.get("id")) or 0, (p.get("alias") or "").strip(), status))
    for tx in seg.get("transactions") or []:
        p = tx.get("player") or {}
        entries.append((to_int(p.get("id")) or 0, (p.get("name") or "").strip(), "transaction"))

    _upsert_team(conn, team_id, "")
    count = 0
    seen: set[tuple[int, str]] = set()
    for player_id, handle, status in entries:
        if not player_id or not handle or (player_id, handle) in seen:
            continue
        seen.add((player_id, handle))
        conn.execute(
            """
            INSERT INTO team_roster_players (team_id, player_id, handle, status)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (team_id, player_id, handle) DO UPDATE SET
                status = EXCLUDED.status, updated_at = now()
            """,
            (team_id, player_id, handle, status),
        )
        count += 1
    return count


def detail_players(body: Any) -> dict[int, set[str]]:
    """Every (team ID -> handles) that appears on a played map of a match-detail body."""
    segs = segments(body)
    if not segs:
        return {}
    seg = segs[0]
    teams = seg.get("teams") or []
    if len(teams) != 2:
        return {}
    ids = {"team1": to_int(teams[0].get("id")), "team2": to_int(teams[1].get("id"))}
    found: dict[int, set[str]] = defaultdict(set)
    for mp in seg.get("maps") or []:
        for key, team_id in ids.items():
            if team_id is None:
                continue
            for p in (mp.get("players") or {}).get(key) or []:
                handle = (p.get("name") or "").strip()
                if handle:
                    found[team_id].add(handle)
    return dict(found)


_LOOKUPS = [
    """
    SELECT DISTINCT player_id FROM event_rosters
    WHERE event_id = %(event_id)s AND team_id = %(team_id)s AND lower(handle) = lower(%(h)s)
    """,
    """
    SELECT DISTINCT player_id FROM team_roster_players
    WHERE team_id = %(team_id)s AND lower(handle) = lower(%(h)s)
    """,
    """
    SELECT DISTINCT player_id FROM map_players
    WHERE team_id = %(team_id)s AND lower(handle) = lower(%(h)s)
    """,
]


def resolve_handles(
    conn: Connection, event_id: int, team_id: int, handles: set[str]
) -> tuple[dict[str, int], set[str]]:
    """Look handles up in the stored rosters. Returns ({handle: player_id}, unresolved).
    Each source is tried in turn; an ambiguous match (two IDs) in one source falls
    through to the next."""
    resolved: dict[str, int] = {}
    for handle in handles:
        for sql in _LOOKUPS:
            rows = conn.execute(
                sql, {"event_id": event_id, "team_id": team_id, "h": handle}
            ).fetchall()
            if len(rows) == 1:
                resolved[handle] = int(rows[0]["player_id"])
                break
    return resolved, handles - resolved.keys()


PlayerIds = dict[tuple[int, str], int]  # (team_id, handle) -> vlr.gg player ID


def match_event_id(conn: Connection, match_id: int) -> int:
    row = conn.execute("SELECT event_id FROM matches WHERE id = %s", (match_id,)).fetchone()
    if row is None:
        raise ParseError(f"match {match_id}: no stub row; sync its event's match list first")
    return int(row["event_id"])


def try_resolve_match(
    conn: Connection, match_id: int, body: Any
) -> tuple[PlayerIds, dict[int, set[str]]]:
    """Resolve every player in a match-detail body from the stored rosters.
    Returns ({(team_id, handle): player_id}, {team_id: unresolved handles})."""
    event_id = match_event_id(conn, match_id)
    ids: PlayerIds = {}
    missing: dict[int, set[str]] = {}
    for team_id, handles in detail_players(body).items():
        resolved, unresolved = resolve_handles(conn, event_id, team_id, handles)
        ids.update({(team_id, h): pid for h, pid in resolved.items()})
        if unresolved:
            missing[team_id] = unresolved
    return ids, missing


def resolve_match(conn: Connection, match_id: int, body: Any) -> PlayerIds:
    """Like try_resolve_match, but raises UnresolvedPlayers if anyone is missing."""
    ids, missing = try_resolve_match(conn, match_id, body)
    if missing:
        raise UnresolvedPlayers(match_id, missing)
    return ids
