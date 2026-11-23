"""Database access with psycopg 3. Plain SQL, no ORM; rows come back as dicts."""

from typing import Any

import psycopg
from psycopg.rows import dict_row

from vct.config import get_settings

Connection = psycopg.Connection[dict[str, Any]]


def connect(url: str | None = None, *, autocommit: bool = False) -> Connection:
    """Open one connection. Use as a context manager: `with connect() as conn:`.

    The pipeline uses autocommit=True and wraps each unit of work (one match, one event
    list) in `with conn.transaction():`, so a failure rolls back only that unit.
    """
    return psycopg.connect(
        url or get_settings().database_url, row_factory=dict_row, autocommit=autocommit
    )
