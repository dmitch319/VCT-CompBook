"""sync_runs bookkeeping: one row per pipeline run, written as it goes so a crashed
run is still visible (status stays 'running')."""

from dataclasses import dataclass, field
from typing import Any

from psycopg.types.json import Jsonb

from vct.db import Connection


@dataclass
class Run:
    conn: Connection
    command: str
    mode: str | None = None  # e.g. 'incremental'; stored in details
    id: int = 0
    events_checked: int = 0
    matches_fetched: int = 0
    matches_failed: int = 0
    showmatches_skipped: int = 0  # completed showmatches seen in match lists, not stored
    errors: dict[str, str] = field(default_factory=dict)
    warnings: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        row = self.conn.execute(
            "INSERT INTO sync_runs (command) VALUES (%s) RETURNING id", (self.command,)
        ).fetchone()
        assert row is not None
        self.id = int(row["id"])
        self.conn.commit()

    def fail_match(self, match_id: int, error: Exception) -> None:
        self.matches_failed += 1
        self.errors[str(match_id)] = f"{type(error).__name__}: {error}"

    def warn(self, match_id: int, issues: list[str]) -> None:
        if issues:
            self.warnings[str(match_id)] = issues

    def finish(self, error: BaseException | None = None) -> str:
        if error is not None:
            status = "failed"
        elif self.matches_failed and self.matches_fetched:
            status = "partial"
        elif self.matches_failed:
            status = "failed"
        else:
            status = "success"
        details: dict[str, Any] = {
            "match_errors": self.errors,
            "warnings": self.warnings,
            "showmatches_skipped": self.showmatches_skipped,
        }
        if self.mode is not None:
            details["mode"] = self.mode
        self.conn.execute(
            """
            UPDATE sync_runs SET
                finished_at = now(), status = %s, events_checked = %s,
                matches_fetched = %s, matches_failed = %s, error = %s, details = %s
            WHERE id = %s
            """,
            (
                status,
                self.events_checked,
                self.matches_fetched,
                self.matches_failed,
                None if error is None else f"{type(error).__name__}: {error}",
                Jsonb(details),
                self.id,
            ),
        )
        self.conn.commit()
        return status
