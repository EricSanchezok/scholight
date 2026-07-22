"""Unified cloud-auth then Scholight migration orchestration."""

from __future__ import annotations

import os
from pathlib import Path

import asyncpg
import structlog

from scholight.db.migrate import _MIGRATION_LOCK_ID, apply_migrations
from scholight.db.migration_policy import migration_checksum, validate_expand_only_sql

logger = structlog.get_logger(__name__)
_AUTH_MIGRATIONS_DIR = Path(
    os.environ.get("AUTH_MIGRATIONS_DIR", Path(__file__).parents[2] / "cloud-auth" / "migrations")
)
_AUTH_MIGRATIONS_TABLE = "_cloud_auth_migrations"
_APPROVED_AUTH_DESTRUCTIVE_CHECKSUMS = frozenset(
    {"054e537099803a432b86567315b1e8f8164d4d064c7607318a3b31df9beb1606"}
)


async def apply_auth_migrations(conn: asyncpg.Connection, migrations_dir: Path) -> None:
    """Apply cloud-auth SQL migrations using the caller-owned connection."""
    sql_files = sorted(migrations_dir.glob("*.sql"))
    if not sql_files:
        msg = f"cloud-auth migration files not found in {migrations_dir}"
        raise FileNotFoundError(msg)

    await conn.execute(
        "CREATE TABLE IF NOT EXISTS _cloud_auth_migrations ("
        "name TEXT PRIMARY KEY, "
        "checksum TEXT, "
        "applied_at TIMESTAMPTZ NOT NULL DEFAULT now()"
        ")"
    )
    await conn.execute("ALTER TABLE _cloud_auth_migrations ADD COLUMN IF NOT EXISTS checksum TEXT")
    rows = await conn.fetch("SELECT name, checksum FROM _cloud_auth_migrations")
    applied = {row["name"]: row["checksum"] for row in rows}

    for filepath in sql_files:
        sql = filepath.read_text(encoding="utf-8")
        checksum = migration_checksum(sql)
        if filepath.name in applied:
            recorded_checksum = applied[filepath.name]
            if recorded_checksum is None:
                await conn.execute(
                    "UPDATE _cloud_auth_migrations SET checksum = $2 WHERE name = $1",
                    filepath.name,
                    checksum,
                )
            elif recorded_checksum != checksum:
                msg = f"applied cloud-auth migration checksum mismatch: {filepath.name}"
                raise RuntimeError(msg)
            logger.debug("cloud-auth migration already applied", name=filepath.name)
            continue

        validate_expand_only_sql(
            sql,
            approved_destructive_checksums=_APPROVED_AUTH_DESTRUCTIVE_CHECKSUMS,
        )
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute(
                "INSERT INTO _cloud_auth_migrations (name, checksum) VALUES ($1, $2)",
                filepath.name,
                checksum,
            )
        logger.info("cloud-auth migration applied", name=filepath.name)


async def run_all_migrations(
    pool: asyncpg.Pool,
    *,
    auth_migrations_dir: Path = _AUTH_MIGRATIONS_DIR,
) -> None:
    """Apply cloud-auth then Scholight migrations under one advisory lock."""
    if not any(auth_migrations_dir.glob("*.sql")):
        msg = f"cloud-auth migration files not found in {auth_migrations_dir}"
        raise FileNotFoundError(msg)

    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_ID)
        try:
            await apply_auth_migrations(conn, auth_migrations_dir)
            await apply_migrations(conn)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_ID)
