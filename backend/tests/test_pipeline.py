"""End-to-end pipeline tests against Postgres, with vlrggapi replaced by a mock transport."""

import copy
import json
import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import fixtures as fx
import httpx
from alembic import command
from alembic.config import Config

from vct.db import Connection
from vct.pipeline.client import VlrClient
from vct.pipeline.discover import discover
from vct.pipeline.reparse import reparse
from vct.pipeline.sync import sync


class FakeVlrggapi:
    """Serves the fixture bodies and counts requests per path. Attributes can be swapped
    between syncs to simulate vlr.gg changing."""

    def __init__(self, detail: dict[str, Any] | None = None) -> None:
        self.calls: Counter[str] = Counter()
        self.details: dict[str, dict[str, Any]] = {
            str(fx.MATCH_ID): detail or fx.match_detail_body()
        }
        self.event_matches = fx.event_matches_body()
        self.event_detail = fx.event_detail_body()
        self.team_rosters = {t: fx.team_roster_body(t) for t in (fx.ALPHA, fx.BRAVO)}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls[path] += 1
        body: dict[str, Any] | None = None
        if path == "/v2/events":
            body = fx.events_body()
        elif path == "/v2/events/matches":
            body = self.event_matches
        elif path == "/v2/match/details":
            body = self.details.get(request.url.params["match_id"])
        elif path == f"/v2/event/{fx.EVENT_ID}":
            assert request.url.params.get("include") == "teams"
            body = self.event_detail
        elif m := re.fullmatch(r"/v2/team/(\d+)/roster", path):
            body = self.team_rosters.get(int(m[1]))
        if body is None:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json=body)


def client_for(conn: Connection, api: FakeVlrggapi) -> VlrClient:
    return VlrClient(
        conn, "http://vlrggapi.test", throttle=False, transport=httpx.MockTransport(api)
    )


def run_full_sync(conn: Connection, api: FakeVlrggapi) -> None:
    with client_for(conn, api) as client:
        discover(conn, client, completed_pages=1)
        sync(conn, client)


def snapshot(conn: Connection) -> dict[str, Any]:
    """Every normalized table's rows, to compare across runs."""
    tables = {
        "events": "id",
        "teams": "id",
        "players": "id",
        "matches": "id",
        "maps": "match_id, map_order",
        "team_maps": "map_id, team_id",
        "map_players": "map_id, player_id",
        "rounds": "map_id, round_number",
        "veto_steps": "match_id, step_order",
    }
    volatile = {"updated_at", "parsed_at", "detail_raw_id"}
    out = {}
    for table, order in tables.items():
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
        out[table] = [{k: v for k, v in r.items() if k not in volatile} for r in rows]
    return out


def test_migrations_roll_back_and_forward(alembic_config: Config, db: Connection) -> None:
    command.downgrade(alembic_config, "base")
    row = db.execute("SELECT to_regclass('raw_responses') AS t").fetchone()
    assert row is not None and row["t"] is None
    command.upgrade(alembic_config, "head")
    row = db.execute("SELECT to_regclass('raw_responses') AS t").fetchone()
    assert row is not None and row["t"] is not None


def test_0002_upgrade_requeues_matches_parsed_with_name_keyed_players(
    alembic_config: Config, db: Connection
) -> None:
    run_full_sync(db, FakeVlrggapi())
    command.downgrade(alembic_config, "0001")  # back to name-keyed players (data cleared)
    db.execute(
        "UPDATE matches SET parsed_at = now();"
        " INSERT INTO players (handle) VALUES ('alp_1');"
        f" INSERT INTO maps (match_id, map_order, map_name, team1_rounds, team2_rounds)"
        f" VALUES ({fx.MATCH_ID}, 1, 'Ascent', 13, 9)"
    )

    command.upgrade(alembic_config, "head")

    assert db.execute("SELECT count(*) AS n FROM maps").fetchone() == {"n": 0}
    assert db.execute("SELECT count(*) AS n FROM players").fetchone() == {"n": 0}
    match = db.execute("SELECT parsed_at FROM matches WHERE id = %s", (fx.MATCH_ID,)).fetchone()
    assert match == {"parsed_at": None}  # the next sync refetches it
    raw = db.execute("SELECT count(*) AS n FROM raw_responses").fetchone()
    assert raw is not None and raw["n"] > 0  # raw layer kept


def test_sync_parses_a_match(db: Connection) -> None:
    api = FakeVlrggapi()
    run_full_sync(db, api)

    events = db.execute("SELECT id, tracked FROM events ORDER BY id").fetchall()
    assert events == [{"id": fx.EVENT_ID, "tracked": True}, {"id": 2002, "tracked": False}]

    match = db.execute("SELECT * FROM matches WHERE id = %s", (fx.MATCH_ID,)).fetchone()
    assert match is not None
    assert (match["team1_id"], match["team2_id"]) == (fx.ALPHA, fx.BRAVO)
    assert (match["team1_score"], match["team2_score"], match["best_of"]) == (2, 0, 3)
    assert match["winner_team_id"] == fx.ALPHA
    assert match["patch"] == "12.04"
    assert match["played_at"] == datetime(2026, 3, 10, 18, tzinfo=UTC)
    assert match["parsed_at"] is not None

    maps = db.execute(
        "SELECT map_order, map_name, vlr_game_id, team1_rounds, team2_rounds,"
        " picked_by_team_id, duration_seconds FROM maps ORDER BY map_order"
    ).fetchall()
    assert [(m["map_name"], m["team1_rounds"], m["team2_rounds"]) for m in maps] == [
        ("Ascent", 13, 9),
        ("Lotus", 13, 11),
    ]
    assert [m["vlr_game_id"] for m in maps] == [5001, 5002]
    assert [m["picked_by_team_id"] for m in maps] == [fx.ALPHA, fx.BRAVO]
    assert maps[0]["duration_seconds"] == 41 * 60 + 7

    comps = db.execute(
        "SELECT map_name, team_id, comp_key, archetype, won, start_side,"
        " rounds_won_attack + rounds_won_defense AS split_total, rounds_won"
        " FROM team_map_comps ORDER BY map_name, team_id"
    ).fetchall()
    assert [(c["map_name"], c["comp_key"], c["archetype"], c["won"]) for c in comps] == [
        ("Ascent", "Fade,Jett,Killjoy,Omen,Sova", "1D-2I-1C-1S", True),
        ("Ascent", "Breach,Cypher,Omen,Raze,Viper", "1D-1I-2C-1S", False),
        ("Lotus", "Fade,Jett,Killjoy,Omen,Sova", "1D-2I-1C-1S", True),
        ("Lotus", "Chamber,Harbor,KAY/O,Neon,Viper", "1D-1I-2C-1S", False),
    ]
    assert [c["start_side"] for c in comps] == ["defense", "attack"] * 2
    assert all(c["split_total"] == c["rounds_won"] for c in comps)

    assert db.execute("SELECT count(*) AS n FROM rounds").fetchone() == {"n": 22 + 24}

    # Players are stored under their vlr.gg IDs: 9 from the event roster, the stand-in
    # from Bravo's transfer log. Only Bravo's roster history was needed.
    players = db.execute("SELECT id, handle FROM players ORDER BY id").fetchall()
    assert {p["handle"]: p["id"] for p in players} == fx.PLAYER_IDS
    assert api.calls[f"/v2/team/{fx.BRAVO}/roster"] == 1
    assert api.calls[f"/v2/team/{fx.ALPHA}/roster"] == 0
    assert api.calls[f"/v2/event/{fx.EVENT_ID}"] == 1
    per_map = db.execute(
        "SELECT count(*) AS n FROM map_players WHERE player_id = %s",
        (fx.PLAYER_IDS[fx.STANDIN],),
    ).fetchone()
    assert per_map == {"n": 2}
    veto = db.execute("SELECT action, team_id, map_name FROM veto_steps ORDER BY step_order")
    assert [tuple(v.values()) for v in veto.fetchall()][-2:] == [
        ("ban", fx.BRAVO, "Icebox"),
        ("decider", None, "Sunset"),
    ]

    pam = db.execute(
        "SELECT handle, agent, role, rating, kast, won FROM player_agent_maps"
        " WHERE handle = 'alp_1' ORDER BY map_name"
    ).fetchall()
    assert [(p["agent"], p["role"], str(p["rating"]), str(p["kast"]), p["won"]) for p in pam] == [
        ("Jett", "Duelist", "1.10", "74.00", True),
    ] * 2

    run = db.execute("SELECT * FROM sync_runs WHERE command = 'sync'").fetchone()
    assert run is not None
    assert run["status"] == "success"
    assert (run["events_checked"], run["matches_fetched"], run["matches_failed"]) == (1, 1, 0)
    assert run["details"]["warnings"] == {}

    raw = db.execute(
        "SELECT body FROM raw_responses WHERE endpoint = '/v2/match/details'"
    ).fetchone()
    assert raw is not None and raw["body"] == fx.match_detail_body()  # stored as received


def test_second_sync_fetches_nothing_new_and_changes_nothing(db: Connection) -> None:
    api = FakeVlrggapi()
    run_full_sync(db, api)
    before = snapshot(db)

    with client_for(db, api) as client:
        sync(db, client)

    assert api.calls["/v2/match/details"] == 1  # completed matches are fetched once
    assert api.calls[f"/v2/event/{fx.EVENT_ID}"] == 1  # no new matches, no roster fetch
    assert snapshot(db) == before


def test_reparse_rebuilds_everything_without_fetching(db: Connection) -> None:
    api = FakeVlrggapi()
    run_full_sync(db, api)
    before = snapshot(db)

    db.execute(
        "TRUNCATE veto_steps, rounds, map_players, team_maps, maps, matches, players,"
        " event_rosters, team_roster_players, teams, events RESTART IDENTITY CASCADE"
    )
    calls_before = sum(api.calls.values())
    run = reparse(db)

    assert run.matches_fetched == 1 and run.matches_failed == 0
    assert sum(api.calls.values()) == calls_before
    after = snapshot(db)
    # maps.id restarts, so compare everything keyed on vlr.gg IDs exactly
    for table in ("events", "teams", "players", "matches", "veto_steps"):
        assert after[table] == before[table], table

    def without_map_id(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{k: v for k, v in r.items() if k != "map_id"} for r in rows]

    assert without_map_id(after["map_players"]) == without_map_id(before["map_players"])
    assert [m["map_name"] for m in after["maps"]] == [m["map_name"] for m in before["maps"]]


def test_unresolved_player_fails_the_match_until_a_roster_has_them(db: Connection) -> None:
    api = FakeVlrggapi()
    api.team_rosters[fx.BRAVO] = fx.team_roster_body(fx.BRAVO, include_standin=False)
    run_full_sync(db, api)

    run = db.execute("SELECT * FROM sync_runs WHERE command = 'sync'").fetchone()
    assert run is not None and run["status"] == "failed"
    error = run["details"]["match_errors"][str(fx.MATCH_ID)]
    assert "UnresolvedPlayers" in error and f"team {fx.BRAVO}: {fx.STANDIN}" in error
    # Nothing stored for the match; it stays queued
    assert db.execute("SELECT count(*) AS n FROM maps").fetchone() == {"n": 0}
    assert db.execute("SELECT count(*) AS n FROM players").fetchone() == {"n": 0}
    assert db.execute("SELECT parsed_at FROM matches WHERE id = %s", (fx.MATCH_ID,)).fetchone() == {
        "parsed_at": None
    }

    # vlr.gg adds the stand-in to the transfer log; the next sync picks the match up
    api.team_rosters[fx.BRAVO] = fx.team_roster_body(fx.BRAVO)
    with client_for(db, api) as client:
        second = sync(db, client)
    assert (second.matches_fetched, second.matches_failed) == (1, 0)
    row = db.execute(
        "SELECT handle FROM players WHERE id = %s", (fx.PLAYER_IDS[fx.STANDIN],)
    ).fetchone()
    assert row == {"handle": fx.STANDIN}


def test_renamed_player_stays_one_player(db: Connection) -> None:
    api = FakeVlrggapi()
    run_full_sync(db, api)

    # alp_1 (ID 101) renames to "alp_one" before their next match
    renamed = dict(fx.PLAYER_IDS)
    renamed["alp_one"] = renamed.pop("alp_1")
    api.event_detail = fx.event_detail_body(renamed)
    api.event_matches = fx.event_matches_body(extra_completed=((1003, "2026-03-12 18:00:00"),))
    api.details["1003"] = fx.renamed_detail_body(1003, "alp_1", "alp_one")
    with client_for(db, api) as client:
        sync(db, client)

    def check() -> None:
        players = db.execute("SELECT id, handle FROM players WHERE id = 101").fetchall()
        assert players == [{"id": 101, "handle": "alp_one"}]  # latest handle
        history = db.execute(
            "SELECT DISTINCT match_id, match_handle FROM player_agent_maps"
            " WHERE player_id = 101 ORDER BY match_id"
        ).fetchall()
        assert [(h["match_id"], h["match_handle"]) for h in history] == [
            (fx.MATCH_ID, "alp_1"),
            (1003, "alp_one"),
        ]
        assert db.execute("SELECT count(*) AS n FROM players").fetchone() == {"n": 10}

    check()

    # A rebuild resolves the old handle too, and the latest handle still wins
    db.execute(
        "TRUNCATE map_players, team_maps, maps, matches, players, event_rosters,"
        " team_roster_players RESTART IDENTITY CASCADE"
    )
    run = reparse(db)
    assert (run.matches_fetched, run.matches_failed) == (2, 0)
    check()


def test_validation_flags_bad_maps_without_failing(db: Connection) -> None:
    detail = copy.deepcopy(fx.match_detail_body())
    seg = detail["data"]["segments"][0]
    seg["maps"][0]["players"]["team2"].pop()  # 4 players
    seg["maps"][1]["players"]["team1"][0]["agent"] = "Mysteryagent"  # unknown role
    seg["maps"][1]["rounds"].pop()  # 23 rounds for a 13-11 map

    run_full_sync(db, FakeVlrggapi(detail))

    run = db.execute("SELECT status, details FROM sync_runs WHERE command = 'sync'").fetchone()
    assert run is not None and run["status"] == "success"
    issues = run["details"]["warnings"][str(fx.MATCH_ID)]
    text = json.dumps(issues)
    assert "4 players, expected 5" in text
    assert "Mysteryagent" in text
    assert "23 rounds recorded, score adds up to 24" in text

    archetype = db.execute(
        "SELECT archetype FROM team_map_comps WHERE map_name = 'Lotus' AND team_id = %s",
        (fx.ALPHA,),
    ).fetchone()
    assert archetype == {"archetype": "0D-2I-1C-1S-1?"}


def test_a_broken_match_is_recorded_and_its_raw_response_kept(db: Connection) -> None:
    detail = copy.deepcopy(fx.match_detail_body())
    detail["data"]["segments"][0]["teams"] = []

    run_full_sync(db, FakeVlrggapi(detail))

    run = db.execute("SELECT * FROM sync_runs WHERE command = 'sync'").fetchone()
    assert run is not None
    assert run["status"] == "failed"
    assert run["matches_failed"] == 1
    assert "expected 2 teams" in run["details"]["match_errors"][str(fx.MATCH_ID)]
    match = db.execute("SELECT parsed_at FROM matches WHERE id = %s", (fx.MATCH_ID,)).fetchone()
    assert match == {"parsed_at": None}  # will be retried by the next sync
    raw = db.execute(
        "SELECT count(*) AS n FROM raw_responses WHERE endpoint = '/v2/match/details'"
    ).fetchone()
    assert raw == {"n": 1}


def test_429_waits_and_retries(db: Connection, monkeypatch: Any) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("vct.pipeline.client.time.sleep", sleeps.append)
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "7"}, json={"detail": "slow down"}),
            httpx.Response(200, json=fx.events_body()),
        ]
    )
    client = VlrClient(
        db,
        "http://vlrggapi.test",
        throttle=False,
        transport=httpx.MockTransport(lambda _req: next(responses)),
    )
    resp = client.get("/v2/events", q="upcoming")

    assert resp.status_code == 200
    assert sleeps == [7.0 + 1.0]  # Retry-After plus a second of slack
    codes = db.execute("SELECT status_code FROM raw_responses ORDER BY id").fetchall()
    assert [c["status_code"] for c in codes] == [429, 200]  # both stored
