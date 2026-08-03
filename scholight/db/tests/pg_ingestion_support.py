"""Fail-closed helpers for destructive ingestion tests on local PostgreSQL."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import asyncpg

from scholight.db.migrate import apply_migrations

_MIGRATIONS = Path(__file__).parents[3] / "migrations"


def isolated_database_url() -> str:
    try:
        url = os.environ["SCHOLIGHT_TEST_DATABASE_URL"]
    except KeyError as exc:
        raise RuntimeError("SCHOLIGHT_TEST_DATABASE_URL is required") from exc
    parsed = urlsplit(url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("ingestion integration tests require a loopback PostgreSQL host")
    database = parsed.path.lstrip("/")
    if not database.startswith("scholight_test_"):
        raise RuntimeError("ingestion integration database must start with scholight_test_")
    return url


async def reset_ingestion_database(pool: asyncpg.Pool) -> None:
    await pool.execute("DROP SCHEMA IF EXISTS scholight CASCADE")
    await pool.execute("DROP SCHEMA IF EXISTS auth CASCADE")
    await pool.execute("CREATE SCHEMA auth")
    await pool.execute(
        "CREATE TABLE auth.users (id BIGINT PRIMARY KEY, email TEXT, email_verified_at TIMESTAMPTZ)"
    )
    await pool.execute("CREATE SCHEMA scholight")
    async with pool.acquire() as connection:
        with patch("scholight.db.migrate._MIGRATIONS_DIR", _MIGRATIONS):
            await apply_migrations(connection)


async def replay_ingestion_migrations(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as connection:
        with patch("scholight.db.migrate._MIGRATIONS_DIR", _MIGRATIONS):
            await apply_migrations(connection)
