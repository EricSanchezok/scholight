"""Scholight-owned schema migration runner."""

from __future__ import annotations

import os
import re
from pathlib import Path

import asyncpg
import structlog
from sanchezcloud_identity.migrate import assert_schema_compatible

from scholight.db.migration_policy import migration_checksum, validate_expand_only_sql

logger = structlog.get_logger(__name__)

_MIGRATIONS_DIR = Path(
    os.environ.get(
        "SCHOLIGHT_MIGRATIONS_DIR",
        Path(__file__).resolve().parent.parent.parent / "migrations",
    )
)
# Stable application-scoped PostgreSQL advisory lock for Scholight migrations.
_MIGRATION_LOCK_ID = 7_192_003_901
# Exact, reviewed contract migrations. Binding the digest to the version and name prevents the
# approval from authorizing different SQL or the same SQL under an unrelated migration identity.
_APPROVED_CONTRACT_MIGRATIONS = {
    (
        4,
        "allow_delegated_usage_actor",
    ): "09d7bc9fc1358cdcdcc66a754992aa72eee31ad550a71f0dde481e612ff45186",
    (
        6,
        "survey_aggregate",
    ): "7839587fb113308b08a29af4453e30d5d51babb90dbf43b720426315fdcaaac9",
    (
        8,
        "survey_cancellation",
    ): "50ba44148b1d14853b2c67ff25f92fe549e8fde208119848789f906002f83a29",
    (
        10,
        "survey_quota_overrides",
    ): "9c07ecf7c8eacafc000442fe369bd3a3c73c0c3ee89a46df119be0e3a195cca7",
}


async def run_migrations(pool: asyncpg.Pool) -> None:
    """Validate independently managed auth, then migrate ``scholight.*``."""
    await assert_schema_compatible(pool)
    async with pool.acquire() as conn:
        owns_schema = await conn.fetchval(
            "SELECT pg_get_userbyid(nspowner) = current_user "
            "FROM pg_namespace WHERE nspname = 'scholight'"
        )
        if owns_schema is not True:
            raise RuntimeError("Scholight schema is missing or not owned by the migration role")
        await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_ID)
        try:
            await apply_migrations(conn)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_ID)


async def apply_migrations(conn: asyncpg.Connection) -> None:
    """Apply pending Scholight migrations using an already-owned connection."""
    sql_files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    if not sql_files:
        msg = f"Scholight migration files not found in {_MIGRATIONS_DIR}"
        raise FileNotFoundError(msg)

    migrations: list[tuple[int, str, Path]] = []
    versions: set[int] = set()
    for filepath in sql_files:
        match = re.fullmatch(r"(\d+)_([a-z0-9][a-z0-9_]*)", filepath.stem)
        if match is None:
            msg = f"invalid Scholight migration filename: {filepath.name}"
            raise ValueError(msg)
        version = int(match.group(1))
        if version in versions:
            msg = f"duplicate Scholight migration version: {version}"
            raise ValueError(msg)
        versions.add(version)
        migrations.append((version, match.group(2), filepath))

    await conn.execute(
        "CREATE TABLE IF NOT EXISTS scholight.schema_migrations ("
        "version INTEGER NOT NULL PRIMARY KEY, "
        "name TEXT NOT NULL, "
        "checksum TEXT NOT NULL, "
        "applied_at TIMESTAMPTZ NOT NULL DEFAULT now()"
        ")"
    )
    for version, name, filepath in migrations:
        sql = filepath.read_text(encoding="utf-8")
        checksum = migration_checksum(sql)
        applied = await conn.fetchrow(
            "SELECT name, checksum FROM scholight.schema_migrations WHERE version = $1",
            version,
        )
        if applied is not None:
            recorded_checksum = applied["checksum"]
            if recorded_checksum != checksum:
                msg = f"applied migration checksum mismatch: {filepath.name}"
                raise RuntimeError(msg)
            logger.debug("migration already applied", version=version, name=name)
            continue

        approved_checksum = _APPROVED_CONTRACT_MIGRATIONS.get((version, name))
        approved_destructive_checksums = (
            frozenset({checksum}) if approved_checksum == checksum else frozenset()
        )
        validate_expand_only_sql(
            sql,
            approved_destructive_checksums=approved_destructive_checksums,
        )
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute(
                "INSERT INTO scholight.schema_migrations "
                "(version, name, checksum) VALUES ($1, $2, $3)",
                version,
                name,
                checksum,
            )

        logger.info("migration applied", version=version, name=name)
