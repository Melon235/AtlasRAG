#!/usr/bin/env python3
"""Fail unless the connected PostgreSQL database is at the single Alembic head."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import AsyncConnection

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def expected_migration_head() -> str:
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(REPOSITORY_ROOT / "migrations"),
    )
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:
        raise RuntimeError("migration history has no head")
    return head


async def database_migration_heads(dsn: str) -> tuple[str, ...]:
    connection = await AsyncConnection.connect(dsn, autocommit=True)
    try:
        cursor = await connection.execute(
            "SELECT version_num FROM alembic_version ORDER BY version_num"
        )
        rows = await cursor.fetchall()
    finally:
        await connection.close()
    return tuple(str(row[0]) for row in rows)


async def check_migration(dsn: str) -> tuple[str, tuple[str, ...]]:
    expected = expected_migration_head()
    actual = await database_migration_heads(dsn)
    return expected, actual


def main() -> int:
    dsn = os.environ.get("ATLASRAG_POSTGRES_DSN", "").strip()
    if not dsn:
        print("ATLASRAG_POSTGRES_DSN is required", file=sys.stderr)
        return 2

    try:
        expected, actual = asyncio.run(check_migration(dsn))
    except Exception as error:
        print(
            f"migration check failed: {type(error).__name__}",
            file=sys.stderr,
        )
        return 1

    if actual != (expected,):
        rendered_actual = ",".join(actual) if actual else "<none>"
        print(
            f"migration revision mismatch: expected={expected} actual={rendered_actual}",
            file=sys.stderr,
        )
        return 1
    print(f"migration head verified: {expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
