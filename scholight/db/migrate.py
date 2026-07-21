"""Migration runner — applies ``migrations/*.sql`` files in sorted order,
tracking applied migrations in a ``_migrations`` table.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import structlog

logger = structlog.get_logger(__name__)

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"
# Stable application-scoped PostgreSQL advisory lock for Scholight migrations.
_MIGRATION_LOCK_ID = 7_192_003_901


async def run_migrations(pool: asyncpg.Pool) -> None:
    """Apply pending SQL migrations under one PostgreSQL advisory lock.

    One acquired connection owns the advisory lock, migration transaction,
    and tracking-table update so concurrent runners cannot race.
    """
    sql_files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    if not sql_files:
        logger.info("no migration files found", dir=str(_MIGRATIONS_DIR))
        return

    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_ID)
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS _migrations ("
                "version INTEGER NOT NULL PRIMARY KEY, "
                "name TEXT NOT NULL, "
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                ")"
            )
            for filepath in sql_files:
                parts = filepath.stem.split("_", 1)
                if len(parts) < 2:
                    logger.warning("skipping unrecognized migration file", file=str(filepath))
                    continue

                try:
                    version = int(parts[0])
                except ValueError:
                    logger.warning("skipping unrecognized migration file", file=str(filepath))
                    continue

                name = parts[1]
                already_applied = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM _migrations WHERE version = $1)", version
                )
                if already_applied:
                    logger.debug("migration already applied", version=version, name=name)
                    continue

                sql = filepath.read_text(encoding="utf-8")
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO _migrations (version, name) VALUES ($1, $2)",
                        version,
                        name,
                    )

                logger.info("migration applied", version=version, name=name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_ID)
