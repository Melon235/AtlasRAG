#!/usr/bin/env python3
"""Wait for the Stage 3 PostgreSQL dependency without exposing credentials."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import sys
import time
from dataclasses import dataclass
from ipaddress import ip_address

from sqlalchemy.engine import make_url

from atlasrag.repositories.postgres.pool import PostgresPool


@dataclass(frozen=True, slots=True)
class PostgresWaitConfig:
    dsn: str
    wait_timeout_seconds: float
    probe_timeout_seconds: float
    poll_seconds: float


def _required_postgres_dsn() -> str:
    name = "ATLASRAG_POSTGRES_DSN"
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    url = make_url(value)
    if url.drivername not in {"postgresql", "postgresql+psycopg"}:
        raise ValueError(f"{name} must use PostgreSQL")
    host = url.host
    if host is None:
        raise ValueError(f"{name} must use a loopback host")
    if host.casefold() != "localhost":
        try:
            address = ip_address(host)
        except ValueError:
            raise ValueError(f"{name} must use a loopback host") from None
        if not address.is_loopback:
            raise ValueError(f"{name} must use a loopback host")
    return value


def _positive_float(name: str, default: str) -> float:
    raw = os.environ.get(name, default)
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a positive finite number") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


def config_from_environment() -> PostgresWaitConfig:
    return PostgresWaitConfig(
        dsn=_required_postgres_dsn(),
        wait_timeout_seconds=_positive_float(
            "ATLASRAG_POSTGRES_WAIT_TIMEOUT_SECONDS",
            "120",
        ),
        probe_timeout_seconds=_positive_float(
            "ATLASRAG_POSTGRES_PROBE_TIMEOUT_SECONDS",
            "2",
        ),
        poll_seconds=_positive_float(
            "ATLASRAG_POSTGRES_POLL_SECONDS",
            "0.5",
        ),
    )


async def _probe(config: PostgresWaitConfig) -> None:
    pool = PostgresPool(
        config.dsn,
        min_size=1,
        max_size=1,
        timeout=config.probe_timeout_seconds,
    )
    primary: BaseException | None = None
    try:
        await pool.open()
        await pool.health()
    except BaseException as error:
        primary = error

    cleanup_error: BaseException | None = None
    try:
        await pool.close()
    except BaseException as error:
        cleanup_error = error

    if isinstance(primary, asyncio.CancelledError):
        if cleanup_error is not None:
            primary.add_note(
                "PostgreSQL readiness cleanup failed after cancellation: "
                f"{type(cleanup_error).__name__}"
            )
        raise primary
    if isinstance(cleanup_error, asyncio.CancelledError):
        raise cleanup_error
    if primary is not None:
        raise primary
    if cleanup_error is not None:
        raise cleanup_error


async def wait_for_postgres(config: PostgresWaitConfig) -> bool:
    deadline = time.monotonic() + config.wait_timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            await asyncio.wait_for(_probe(config), timeout=remaining)
        except TimeoutError:
            return False
        except Exception:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(config.poll_seconds, remaining))
        else:
            return time.monotonic() < deadline


async def _async_main() -> int:
    logging.getLogger("psycopg.pool").setLevel(logging.CRITICAL + 1)
    try:
        config = config_from_environment()
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    if await wait_for_postgres(config):
        print("PostgreSQL ready")
        return 0
    print("PostgreSQL unavailable before timeout", file=sys.stderr)
    return 1


def main() -> int:
    return asyncio.run(_async_main())


if __name__ == "__main__":
    raise SystemExit(main())
