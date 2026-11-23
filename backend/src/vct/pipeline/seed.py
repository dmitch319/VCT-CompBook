"""Load hand-maintained reference data from seed/ into the database."""

import csv
from pathlib import Path

from vct.db import Connection
from vct.pipeline.parse import refresh_comps

# backend/src/vct/pipeline/seed.py -> repo root
REPO_ROOT = Path(__file__).resolve().parents[4]
AGENT_ROLES_CSV = REPO_ROOT / "seed" / "agent_roles.csv"


def load_agents(conn: Connection, path: Path = AGENT_ROLES_CSV) -> int:
    """Upsert agents from the CSV, then recompute archetypes in case a role changed."""
    with path.open(newline="", encoding="utf-8") as f:
        rows = [(r["name"].strip(), r["role"].strip()) for r in csv.DictReader(f) if r["name"]]
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO agents (name, role) VALUES (%s, %s)
                ON CONFLICT (name) DO UPDATE SET role = EXCLUDED.role
                """,
                rows,
            )
        refresh_comps(conn)
    return len(rows)


PLAYER_OVERRIDES_CSV = REPO_ROOT / "seed" / "player_overrides.csv"
MATCH_EXCLUSIONS_CSV = REPO_ROOT / "seed" / "match_exclusions.csv"


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [{k: (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]


def load_player_overrides(conn: Connection, path: Path = PLAYER_OVERRIDES_CSV) -> int:
    """Replace player_overrides with the CSV (team_id, handle, player_id, note)."""
    rows = [
        (int(r["team_id"]), r["handle"], int(r["player_id"]), r.get("note") or None)
        for r in _read_csv(path)
        if r.get("team_id") and r.get("handle") and r.get("player_id")
    ]
    with conn.transaction():
        conn.execute("DELETE FROM player_overrides")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO player_overrides (team_id, handle, player_id, note)"
                " VALUES (%s, %s, %s, %s)",
                rows,
            )
    return len(rows)


def load_match_exclusions(conn: Connection, path: Path = MATCH_EXCLUSIONS_CSV) -> int:
    """Replace match_exclusions with the CSV (match_id, reason), and remove anything
    already parsed for an excluded match so it drops out of the analysis."""
    rows = [
        (int(r["match_id"]), r["reason"])
        for r in _read_csv(path)
        if r.get("match_id") and r.get("reason")
    ]
    with conn.transaction():
        conn.execute("DELETE FROM match_exclusions")
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO match_exclusions (match_id, reason) VALUES (%s, %s)", rows)
        excluded = "SELECT match_id FROM match_exclusions"
        conn.execute(f"DELETE FROM maps WHERE match_id IN ({excluded})")
        conn.execute(f"DELETE FROM veto_steps WHERE match_id IN ({excluded})")
        conn.execute(f"UPDATE matches SET parsed_at = NULL WHERE id IN ({excluded})")
    return len(rows)


def load_seed(conn: Connection) -> dict[str, int]:
    """Everything in seed/: agents, player overrides, match exclusions."""
    return {
        "agents": load_agents(conn),
        "player_overrides": load_player_overrides(conn),
        "match_exclusions": load_match_exclusions(conn),
    }
