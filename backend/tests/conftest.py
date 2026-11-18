"""Shared fixtures.

Database tests run against TEST_DATABASE_URL and are skipped when it isn't set. They
drop and recreate the whole schema, so never point it at a database you care about:

    docker compose up -d db
    docker compose exec db createdb -U vct vct_test
    TEST_DATABASE_URL=postgresql://vct:<password>@localhost:5432/vct_test pytest
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from vct.db import Connection, connect

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


def _alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    sa_url = url.replace("postgresql://", "postgresql+psycopg://", 1).replace(
        "postgres://", "postgresql+psycopg://", 1
    )
    cfg.attributes["sqlalchemy_url"] = sa_url
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session")
def alembic_config() -> Config:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    return _alembic_config(TEST_DATABASE_URL)


@pytest.fixture
def db(alembic_config: Config) -> Iterator[Connection]:
    """A freshly migrated, empty database with agents seeded."""
    from vct.pipeline.seed import load_agents

    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")
    with connect(TEST_DATABASE_URL, autocommit=True) as conn:
        load_agents(conn)
        yield conn
