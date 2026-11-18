"""End-to-end pipeline tests against Postgres, with vlrggapi replaced by a mock transport."""

import copy
import json
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
    """Serves the fixture bodies and counts requests per endpoint."""

    def __init__(self, detail: dict[str, Any] | None = None) -> None:
        self.calls: Counter[str] = Counter()
        self.detail = detail or fx.match_detail_body()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls[path] += 1
        if path == "/v2/events":
            return httpx.Response(200, json=fx.events_body())
        if path == "/v2/events/matches":
            return httpx.Response(200, json=fx.event_matches_body())
        if path == "/v2/match/details":
            return httpx.Response(200, json=self.detail)
        return httpx.Response(404, json={"detail": "not found"})


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
        "players": "handle",
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
    assert db.execute("SELECT count(*) AS n FROM players").fetchone() == {"n": 10}
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
    assert snapshot(db) == before


def test_reparse_rebuilds_everything_without_fetching(db: Connection) -> None:
    api = FakeVlrggapi()
    run_full_sync(db, api)
    before = snapshot(db)

    db.execute(
        "TRUNCATE veto_steps, rounds, map_players, team_maps, maps, matches, players,"
        " teams, events RESTART IDENTITY CASCADE"
    )
    calls_before = sum(api.calls.values())
    run = reparse(db)

    assert run.matches_fetched == 1 and run.matches_failed == 0
    assert sum(api.calls.values()) == calls_before
    after = snapshot(db)
    # Identity values restart, so compare everything but surrogate keys
    for table in ("events", "teams", "matches", "veto_steps"):
        assert after[table] == before[table], table
    assert len(after["map_players"]) == len(before["map_players"])
    assert [m["map_name"] for m in after["maps"]] == [m["map_name"] for m in before["maps"]]


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
    assert sleeps == [7.0]
    codes = db.execute("SELECT status_code FROM raw_responses ORDER BY id").fetchall()
    assert [c["status_code"] for c in codes] == [429, 200]  # both stored
