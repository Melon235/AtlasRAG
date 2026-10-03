"""Explicit append-only migrations, independent of application imports."""

import os

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import URL, make_url

target_metadata = None


def database_url() -> URL:
    dsn = os.environ.get("ATLASRAG_POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("ATLASRAG_POSTGRES_DSN is required for migrations")
    url = make_url(dsn)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
