"""Alembic environment. Migrations are raw SQL in op.execute(); there are no ORM models,
so autogenerate is not used."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from vct.config import get_settings

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Tests pass an explicit URL through config attributes; everything else uses DATABASE_URL.
url = config.attributes.get("sqlalchemy_url") or get_settings().sqlalchemy_url


def run_migrations_offline() -> None:
    """Print the SQL instead of running it (`alembic upgrade head --sql`)."""
    context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
