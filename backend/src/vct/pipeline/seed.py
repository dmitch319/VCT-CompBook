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
