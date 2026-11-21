"""Turn stored vlrggapi responses into normalized rows.

Every function here takes a decoded response body (as stored in raw_responses.body), so
the same code runs during `sync` and `reparse`. All writes are upserts keyed on vlr.gg
IDs, or delete-and-reinsert within one match, so parsing the same body twice changes
nothing.

Response shapes follow vlrggapi at the commit pinned in docker-compose.yml:
  /v2/events          data.segments[] = {event_id, title, status, dates, region, ...}
  /v2/events/matches  data.segments[] = {match_id, status, stage, event_series,
                                         unix_timestamp, team1, team2, ...}
  /v2/match/details   data.segments[0] = {teams[2], date, map_vetos, maps[], performance}
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from vct.db import Connection

# ------------------------------------------------------------------ value helpers


def to_int(value: Any) -> int | None:
    """'13' -> 13, 13 -> 13, '' / None / 'TBD' -> None. Ignores a trailing '%' and a '+'."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().rstrip("%").lstrip("+").replace("\xa0", "")
    try:
        return int(text)
    except ValueError:
        return None


def to_decimal(value: Any) -> Decimal | None:
    """'1.23' -> Decimal('1.23'), '75%' -> Decimal('75'), '' -> None."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().rstrip("%").lstrip("+").replace("\xa0", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_duration(text: Any) -> int | None:
    """'45:12' -> 2712, '1:02:33' -> 3753, '' -> None."""
    if not text:
        return None
    parts = str(text).strip().split(":")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def parse_utc_timestamp(text: Any) -> datetime | None:
    """vlrggapi's unix_timestamp field, which is actually '%Y-%m-%d %H:%M:%S' in UTC."""
    if not text:
        return None
    try:
        return datetime.strptime(str(text).strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


_PATCH_RE = re.compile(r"\bPatch\s*(\d+(?:\.\d+)+)", re.IGNORECASE)


def find_patch(*texts: Any) -> str | None:
    for text in texts:
        if text and (m := _PATCH_RE.search(str(text))):
            return m.group(1)
    return None


def best_of_from_score(score1: int | None, score2: int | None) -> int | None:
    """A Bo3 is won with 2 maps, a Bo5 with 3, so best-of = 2 * winner's maps - 1."""
    if score1 is None or score2 is None or score1 == score2:
        return None
    bo = 2 * max(score1, score2) - 1
    return bo if bo in (1, 3, 5) else None


# ------------------------------------------------------------------ event filtering

_EXCLUDE_RE = re.compile(
    r"challengers|game changers|ascension|qualifier|premier|academy|showmatch|"
    r"all[- ]star|invitational|stage \d+:? (open|closed)",
    re.IGNORECASE,
)
_TIER1_RES = [
    # "Champions Tour 2024: Americas Stage 1", "VCT 2026: EMEA Kickoff",
    # "Champions Tour 2024: Masters Madrid", "Champions Tour 2023: Champions Los Angeles"
    re.compile(
        r"^(valorant\s+)?(champions tour|vct)\s+\d{4}\s*:\s*"
        r"(americas|emea|pacific|china|masters|champions)\b",
        re.IGNORECASE,
    ),
    # "Valorant Masters Toronto 2025", "Valorant Champions 2025"
    re.compile(r"^valorant\s+(masters|champions)\b", re.IGNORECASE),
]


def is_tier1_event(name: str) -> bool:
    """Tier 1 VCT: the four international leagues, Masters and Champions (design issue 6).
    Anything this misses can be tracked by hand with `vct-pipeline track <event_id>`."""
    name = name.strip()
    if _EXCLUDE_RE.search(name):
        return False
    return any(r.search(name) for r in _TIER1_RES)


# ------------------------------------------------------------------ envelope helpers


def segments(body: Any) -> list[dict[str, Any]]:
    """data.segments from a vlrggapi v2 response body ([] if missing)."""
    if not isinstance(body, dict):
        return []
    data = body.get("data")
    if not isinstance(data, dict):
        return []
    segs = data.get("segments")
    return [s for s in segs if isinstance(s, dict)] if isinstance(segs, list) else []


class ParseError(ValueError):
    pass


# ------------------------------------------------------------------ /v2/events


def parse_events(conn: Connection, body: Any) -> int:
    """Upsert events from a /v2/events response. New tier-1 events start tracked; an
    event that is already tracked (e.g. by hand) stays tracked. Returns rows upserted."""
    count = 0
    for ev in segments(body):
        event_id = to_int(ev.get("event_id"))
        name = (ev.get("title") or "").strip()
        if event_id is None or not name:
            continue
        conn.execute(
            """
            INSERT INTO events (id, name, region, status, dates_label, tracked)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name        = EXCLUDED.name,
                region      = COALESCE(EXCLUDED.region, events.region),
                status      = COALESCE(EXCLUDED.status, events.status),
                dates_label = COALESCE(EXCLUDED.dates_label, events.dates_label),
                tracked     = events.tracked OR EXCLUDED.tracked,
                updated_at  = now()
            """,
            (
                event_id,
                name,
                ev.get("region") or None,
                (ev.get("status") or "").strip().lower() or None,
                ev.get("dates") or None,
                is_tier1_event(name),
            ),
        )
        count += 1
    return count


# ------------------------------------------------------------------ /v2/events/matches


@dataclass(frozen=True)
class ListedMatch:
    match_id: int
    completed: bool


def parse_event_matches(conn: Connection, event_id: int, body: Any) -> list[ListedMatch]:
    """Upsert a stub row per listed match (event, stage, time, status). Team IDs and
    scores come later from the match detail. Returns the listed matches."""
    listed: list[ListedMatch] = []
    for m in segments(body):
        match_id = to_int(m.get("match_id"))
        if match_id is None:
            continue
        status = (m.get("status") or "").strip() or None
        conn.execute(
            """
            INSERT INTO matches (id, event_id, stage, series, played_at, status)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                event_id   = EXCLUDED.event_id,
                stage      = COALESCE(EXCLUDED.stage, matches.stage),
                series     = COALESCE(EXCLUDED.series, matches.series),
                played_at  = COALESCE(EXCLUDED.played_at, matches.played_at),
                status     = COALESCE(EXCLUDED.status, matches.status),
                updated_at = now()
            """,
            (
                match_id,
                event_id,
                (m.get("stage") or "").strip() or None,
                (m.get("event_series") or "").strip() or None,
                parse_utc_timestamp(m.get("unix_timestamp")),
                status,
            ),
        )
        listed.append(ListedMatch(match_id, (status or "").lower() == "completed"))
    return listed


# ------------------------------------------------------------------ /v2/match/details

_SIDE = {"ct": "defense", "t": "attack"}
_OTHER_SIDE = {"attack": "defense", "defense": "attack"}
_VETO_RE = re.compile(r"^(?P<team>.+?)\s+(?P<action>ban|pick)\s+(?P<map>.+)$", re.IGNORECASE)
_DECIDER_RE = re.compile(r"^(?P<map>.+?)\s+remains$", re.IGNORECASE)


@dataclass(frozen=True)
class VetoStep:
    team: str | None  # team tag/name as written on vlr.gg; None for the decider
    action: str  # ban / pick / decider
    map_name: str


def parse_veto_text(text: str | None) -> list[VetoStep]:
    """'FNC ban Bind; PRX ban Haven; FNC pick Split; ...; Sunset remains' -> steps."""
    steps: list[VetoStep] = []
    for part in (text or "").split(";"):
        part = part.strip()
        if not part:
            continue
        if m := _VETO_RE.match(part):
            steps.append(VetoStep(m["team"].strip(), m["action"].lower(), m["map"].strip()))
        elif m := _DECIDER_RE.match(part):
            steps.append(VetoStep(None, "decider", m["map"].strip()))
    return steps


@dataclass
class TeamRef:
    id: int
    name: str
    tag: str

    def matches(self, label: str) -> bool:
        label = label.strip().casefold()
        return bool(label) and label in (self.name.casefold(), self.tag.casefold())


@dataclass
class MatchParseResult:
    match_id: int
    map_ids: list[int] = field(default_factory=list)


def _upsert_player(conn: Connection, player_id: int, handle: str, seen_at: datetime | None) -> None:
    """players.handle follows the most recent match a player appears in, whatever order
    matches are parsed in, so a renamed player shows their current handle."""
    conn.execute(
        """
        INSERT INTO players (id, handle, last_seen_at) VALUES (%s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            handle       = EXCLUDED.handle,
            last_seen_at = EXCLUDED.last_seen_at,
            updated_at   = now()
        WHERE players.last_seen_at IS NULL
           OR EXCLUDED.last_seen_at >= players.last_seen_at
        """,
        (player_id, handle, seen_at),
    )


def parse_match_detail(
    conn: Connection,
    match_id: int,
    body: Any,
    player_ids: Mapping[tuple[int, str], int],
    raw_id: int | None = None,
) -> MatchParseResult:
    """Write one match's teams, maps, per-map team rows, player stats, rounds and veto.

    The match must already exist as a stub from parse_event_matches (that is where its
    event comes from). `player_ids` maps (team_id, handle) to the vlr.gg player ID, from
    rosters.resolve_match; a player missing from it is an error. Run inside a
    transaction: on error nothing is half-written.
    """
    segs = segments(body)
    if not segs:
        raise ParseError(f"match {match_id}: response has no segments")
    seg = segs[0]

    teams_in = seg.get("teams") or []
    if len(teams_in) != 2:
        raise ParseError(f"match {match_id}: expected 2 teams, got {len(teams_in)}")
    teams: list[TeamRef] = []
    for t in teams_in:
        team_id = to_int(t.get("id"))
        name = (t.get("name") or "").strip()
        if team_id is None or not name:
            raise ParseError(f"match {match_id}: team without an ID or name: {t!r}")
        teams.append(TeamRef(team_id, name, (t.get("tag") or "").strip()))
        conn.execute(
            """
            INSERT INTO teams (id, name, tag) VALUES (%s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                tag = COALESCE(EXCLUDED.tag, teams.tag),
                updated_at = now()
            """,
            (team_id, name, teams[-1].tag or None),
        )
    t1, t2 = teams

    def team_for(label: str | None) -> int | None:
        if not label:
            return None
        return next((t.id for t in teams if t.matches(label)), None)

    score1, score2 = to_int(teams_in[0].get("score")), to_int(teams_in[1].get("score"))
    winner = (
        t1.id if teams_in[0].get("is_winner") else t2.id if teams_in[1].get("is_winner") else None
    )
    veto_text = (seg.get("map_vetos") or "").strip() or None
    patch = find_patch(seg.get("date"), veto_text)

    match_row = conn.execute(
        """
        UPDATE matches SET
            team1_id = %s, team2_id = %s, team1_score = %s, team2_score = %s,
            winner_team_id = %s, best_of = %s, patch = %s, veto_text = %s,
            detail_raw_id = COALESCE(%s, detail_raw_id), parsed_at = now(), updated_at = now()
        WHERE id = %s
        RETURNING played_at
        """,
        (
            t1.id,
            t2.id,
            score1,
            score2,
            winner,
            best_of_from_score(score1, score2),
            patch,
            veto_text,
            raw_id,
            match_id,
        ),
    ).fetchone()
    if match_row is None:
        raise ParseError(f"match {match_id}: no stub row; sync its event's match list first")
    played_at: datetime | None = match_row["played_at"]

    # Game (map) IDs only appear in the per-map performance tab list
    by_map = (seg.get("performance") or {}).get("by_map") or []
    game_ids = [to_int(g.get("game_id")) for g in by_map if isinstance(g, dict)]

    result = MatchParseResult(match_id)
    for index, mp in enumerate(seg.get("maps") or []):
        score = mp.get("score") or {}
        r1, r2 = to_int(score.get("team1")), to_int(score.get("team2"))
        if r1 is None or r2 is None:
            continue  # listed but not played (e.g. map 3 of a 2-0)
        map_order = len(result.map_ids) + 1
        map_winner = t1.id if r1 > r2 else t2.id if r2 > r1 else None

        row = conn.execute(
            """
            INSERT INTO maps (match_id, map_order, vlr_game_id, map_name, patch,
                              picked_by_team_id, duration_seconds, team1_rounds,
                              team2_rounds, winner_team_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (match_id, map_order) DO UPDATE SET
                vlr_game_id       = EXCLUDED.vlr_game_id,
                map_name          = EXCLUDED.map_name,
                patch             = EXCLUDED.patch,
                picked_by_team_id = EXCLUDED.picked_by_team_id,
                duration_seconds  = EXCLUDED.duration_seconds,
                team1_rounds      = EXCLUDED.team1_rounds,
                team2_rounds      = EXCLUDED.team2_rounds,
                winner_team_id    = EXCLUDED.winner_team_id
            RETURNING id
            """,
            (
                match_id,
                map_order,
                game_ids[index] if index < len(game_ids) else None,
                (mp.get("map_name") or "").strip() or "Unknown",
                patch,
                team_for(mp.get("picked_by")),
                parse_duration(mp.get("duration")),
                r1,
                r2,
                map_winner,
            ),
        ).fetchone()
        assert row is not None
        map_id = int(row["id"])
        result.map_ids.append(map_id)

        for table in ("rounds", "map_players", "team_maps"):
            conn.execute(f"DELETE FROM {table} WHERE map_id = %s", (map_id,))

        # Rounds, and each team's starting side from who won round 1 on which side
        team_by_key = {"team1": t1.id, "team2": t2.id}
        round_rows = []
        start_side: dict[int, str] = {}
        for rd in mp.get("rounds") or []:
            number = to_int(rd.get("round_num"))
            if number is None:
                continue
            side = _SIDE.get((rd.get("side") or "").lower())
            winner_id = team_by_key.get(rd.get("winner") or "")
            round_rows.append(
                (map_id, number, winner_id, side, (rd.get("win_type") or "").strip() or None)
            )
            if number == 1 and side and winner_id:
                loser_id = t2.id if winner_id == t1.id else t1.id
                start_side = {winner_id: side, loser_id: _OTHER_SIDE[side]}
        if round_rows:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO rounds (map_id, round_number, winner_team_id, winner_side,
                                        win_type)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    round_rows,
                )

        for key, team, rounds_won in (("team1", t1, r1), ("team2", t2, r2)):
            conn.execute(
                """
                INSERT INTO team_maps (map_id, team_id, rounds_won, rounds_won_attack,
                                       rounds_won_defense, rounds_won_ot, start_side, won)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    map_id,
                    team.id,
                    rounds_won,
                    to_int((mp.get("score_t") or {}).get(key)),
                    to_int((mp.get("score_ct") or {}).get(key)),
                    to_int((mp.get("score_ot") or {}).get(key)),
                    start_side.get(team.id),
                    map_winner == team.id,
                ),
            )
            for p in (mp.get("players") or {}).get(key) or []:
                handle = (p.get("name") or "").strip()
                if not handle:
                    continue
                player_id = player_ids.get((team.id, handle))
                if player_id is None:
                    raise ParseError(
                        f"match {match_id}: no vlr.gg player ID for {handle!r} (team {team.id})"
                    )
                _upsert_player(conn, player_id, handle, played_at)
                conn.execute(
                    """
                    INSERT INTO map_players (map_id, player_id, team_id, handle, agent, rating,
                        acs, kills, deaths, assists, kast, adr, hs_pct, first_kills, first_deaths)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (map_id, player_id) DO NOTHING
                    """,
                    (
                        map_id,
                        player_id,
                        team.id,
                        handle,
                        (p.get("agent") or "").strip(),
                        to_decimal(p.get("rating")),
                        to_int(p.get("acs")),
                        to_int(p.get("kills")),
                        to_int(p.get("deaths")),
                        to_int(p.get("assists")),
                        to_decimal(p.get("kast")),
                        to_decimal(p.get("adr")),
                        to_decimal(p.get("hs_pct")),
                        to_int(p.get("fk")),
                        to_int(p.get("fd")),
                    ),
                )

    # Maps from an earlier parse that this one no longer has
    conn.execute(
        "DELETE FROM maps WHERE match_id = %s AND map_order > %s",
        (match_id, len(result.map_ids)),
    )

    conn.execute("DELETE FROM veto_steps WHERE match_id = %s", (match_id,))
    veto = parse_veto_text(veto_text)
    if veto:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO veto_steps (match_id, step_order, team_id, action, map_name)
                VALUES (%s, %s, %s, %s, %s)
                """,
                [
                    (match_id, i, team_for(s.team), s.action, s.map_name)
                    for i, s in enumerate(veto, start=1)
                ],
            )

    refresh_comps(conn, result.map_ids)
    return result


def refresh_comps(conn: Connection, map_ids: list[int] | None = None) -> None:
    """Set team_maps.comp_key (agents sorted, comma-joined) and archetype
    ('<n>D-<n>I-<n>C-<n>S', plus '-<n>?' for agents missing from the agents table)
    from map_players. With map_ids=None, recomputes every map (e.g. after agents change).
    """
    conn.execute(
        """
        UPDATE team_maps tm
        SET comp_key = c.comp_key, archetype = c.archetype
        FROM (
            SELECT
                pl.map_id,
                pl.team_id,
                string_agg(pl.agent, ',' ORDER BY lower(pl.agent)) AS comp_key,
                format('%%sD-%%sI-%%sC-%%sS',
                       count(*) FILTER (WHERE a.role = 'Duelist'),
                       count(*) FILTER (WHERE a.role = 'Initiator'),
                       count(*) FILTER (WHERE a.role = 'Controller'),
                       count(*) FILTER (WHERE a.role = 'Sentinel'))
                || CASE WHEN count(*) FILTER (WHERE a.role IS NULL) > 0
                        THEN format('-%%s?', count(*) FILTER (WHERE a.role IS NULL))
                        ELSE '' END AS archetype
            FROM map_players pl
            LEFT JOIN agents a ON lower(a.name) = lower(pl.agent)
            WHERE %(all)s OR pl.map_id = ANY(%(ids)s)
            GROUP BY pl.map_id, pl.team_id
        ) c
        WHERE tm.map_id = c.map_id AND tm.team_id = c.team_id
        """,
        {"all": map_ids is None, "ids": map_ids or []},
    )
