"""Settings from environment variables.

A `.env` file in the current directory or any parent (normally the repo root) is loaded
first, so local runs pick up the values from `.env.example` -> `.env`. Real environment
variables (e.g. GitHub Actions secrets) always win over the file.
"""

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import find_dotenv, load_dotenv


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    database_url: str
    vlrggapi_url: str

    @property
    def sqlalchemy_url(self) -> str:
        """The same database as a SQLAlchemy URL (psycopg 3 driver), for Alembic."""
        url = self.database_url
        for prefix in ("postgresql://", "postgres://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix) :]
        return url


@lru_cache
def get_settings() -> Settings:
    load_dotenv(find_dotenv(usecwd=True), override=False)
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ConfigError("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")
    return Settings(
        database_url=database_url,
        vlrggapi_url=os.environ.get("VLRGGAPI_URL", "http://localhost:3001").rstrip("/"),
    )
