"""Schema rules, checked against the source so they can't silently regress:

1. The schema comes only from Alembic migrations: no `CREATE ... IF NOT EXISTS` in app
   code or migrations (that pattern hides drift between databases).
2. Every `INSERT ... ON CONFLICT (cols)` names a target that is backed by a primary key,
   unique constraint or plain unique index on exactly those columns, so Postgres can
   use it as an arbiter. A target-less `ON CONFLICT DO NOTHING` is not allowed either,
   since it swallows every conflict, including ones we'd want to hear about.
"""

import re
from pathlib import Path

import pytest

from vct.db import Connection

BACKEND_DIR = Path(__file__).resolve().parents[1]
SQL_SOURCES = sorted(
    [*(BACKEND_DIR / "src").rglob("*.py"), *(BACKEND_DIR / "migrations").rglob("*.py")]
)

_IF_NOT_EXISTS_RE = re.compile(r"\bCREATE\b[^;]*?\bIF\s+NOT\s+EXISTS\b", re.IGNORECASE)
# INSERT INTO <table> ... ON CONFLICT [(<cols>)], within one SQL string literal: the match
# may not cross a ';' or a '"', so an INSERT without ON CONFLICT can't borrow the clause of
# the next query in the file.
_UPSERT_RE = re.compile(
    r"INSERT\s+INTO\s+(?P<table>\w+)[^;\"]*?ON\s+CONFLICT\s*(?:\((?P<cols>[^)]*)\))?",
    re.IGNORECASE,
)


def upserts() -> list[tuple[str, str, frozenset[str] | None]]:
    """(location, table, target columns or None) for every ON CONFLICT in the source."""
    found = []
    for path in SQL_SOURCES:
        text = path.read_text(encoding="utf-8")
        for m in _UPSERT_RE.finditer(text):
            line = text.count("\n", 0, m.start("table")) + 1
            cols = m.group("cols")
            target = frozenset(c.strip().lower() for c in cols.split(",")) if cols else None
            found.append((f"{path.relative_to(BACKEND_DIR)}:{line}", m["table"].lower(), target))
    return found


def test_no_create_if_not_exists() -> None:
    offenders = [
        str(p.relative_to(BACKEND_DIR))
        for p in SQL_SOURCES
        if _IF_NOT_EXISTS_RE.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], "create schema objects in an Alembic migration instead"


def test_upserts_are_found() -> None:
    # Guards the regex: if it stops matching, the arbiter test below would pass vacuously
    assert len(upserts()) >= 8


@pytest.mark.parametrize(("where", "table", "target"), upserts(), ids=lambda v: str(v))
def test_on_conflict_target_has_unique_constraint(
    db: Connection, where: str, table: str, target: frozenset[str] | None
) -> None:
    assert target is not None, f"{where}: ON CONFLICT on {table} needs an explicit target"
    rows = db.execute(
        """
        SELECT array_agg(a.attname::text) AS cols
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
        WHERE i.indrelid = %s::regclass
          AND i.indisunique
          AND i.indpred IS NULL          -- partial indexes need a matching WHERE
          AND i.indexprs IS NULL         -- expression indexes can't match a column list
        GROUP BY i.indexrelid
        """,
        (table,),
    ).fetchall()
    unique_sets = {frozenset(r["cols"]) for r in rows}
    assert target in unique_sets, (
        f"{where}: ON CONFLICT ({', '.join(sorted(target))}) on {table} has no matching "
        f"unique constraint; unique column sets are {sorted(map(sorted, unique_sets))}"
    )
