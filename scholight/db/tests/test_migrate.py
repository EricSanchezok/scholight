"""Migration runner concurrency and transaction ownership tests."""

from __future__ import annotations

import hashlib
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from types import TracebackType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scholight.db.migrate import _MIGRATION_LOCK_ID, apply_migrations, run_migrations
from scholight.db.migration_policy import validate_expand_only_sql


class _AsyncContext(AbstractAsyncContextManager[MagicMock]):
    def __init__(self, value: MagicMock) -> None:
        self.value = value

    async def __aenter__(self) -> MagicMock:
        return self.value

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return None


class _Transaction(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return None


@pytest.mark.asyncio
async def test_run_migrations_holds_advisory_lock_and_owns_transaction(tmp_path: Path) -> None:
    migration = tmp_path / "001_create_example.sql"
    migration.write_text("CREATE TABLE example (id INTEGER PRIMARY KEY);", encoding="utf-8")

    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=True)
    conn.fetchrow = AsyncMock(return_value=None)
    conn.transaction.return_value = _Transaction()
    pool = MagicMock()
    pool.acquire.return_value = _AsyncContext(conn)

    with (
        patch("scholight.db.migrate._MIGRATIONS_DIR", tmp_path),
        patch("scholight.db.migrate.assert_schema_compatible", new_callable=AsyncMock),
    ):
        await run_migrations(pool)

    pool.acquire.assert_called_once_with()
    assert conn.execute.await_args_list[0].args == (
        "SELECT pg_advisory_lock($1)",
        _MIGRATION_LOCK_ID,
    )
    assert conn.execute.await_args_list[-1].args == (
        "SELECT pg_advisory_unlock($1)",
        _MIGRATION_LOCK_ID,
    )
    conn.transaction.assert_called_once_with()
    assert any(
        call.args == (migration.read_text(encoding="utf-8"),)
        for call in conn.execute.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("owns_schema", [False, None])
async def test_run_migrations_requires_preprovisioned_owned_product_schema(
    tmp_path: Path,
    owns_schema: bool | None,
) -> None:
    (tmp_path / "001_create_example.sql").write_text("SELECT 1;", encoding="utf-8")
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=owns_schema)
    pool = MagicMock()
    pool.acquire.return_value = _AsyncContext(conn)

    with (
        patch("scholight.db.migrate._MIGRATIONS_DIR", tmp_path),
        patch("scholight.db.migrate.assert_schema_compatible", new_callable=AsyncMock),
        pytest.raises(RuntimeError, match="missing or not owned"),
    ):
        await run_migrations(pool)

    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_applied_migration_checksum_mismatch_fails_closed(tmp_path: Path) -> None:
    migration = tmp_path / "001_create_example.sql"
    migration.write_text("CREATE TABLE example (id INTEGER PRIMARY KEY);", encoding="utf-8")
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=True)
    conn.fetchrow = AsyncMock(return_value={"name": "create_example", "checksum": "0" * 64})
    conn.transaction.return_value = _Transaction()
    pool = MagicMock()
    pool.acquire.return_value = _AsyncContext(conn)

    with (
        patch("scholight.db.migrate._MIGRATIONS_DIR", tmp_path),
        patch("scholight.db.migrate.assert_schema_compatible", new_callable=AsyncMock),
        pytest.raises(RuntimeError, match="checksum mismatch"),
    ):
        await run_migrations(pool)


@pytest.mark.asyncio
async def test_applied_migration_without_checksum_fails_closed(tmp_path: Path) -> None:
    migration = tmp_path / "001_create_example.sql"
    migration.write_text("CREATE TABLE example (id INTEGER PRIMARY KEY);", encoding="utf-8")
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=True)
    conn.fetchrow = AsyncMock(return_value={"name": "create_example", "checksum": None})
    conn.transaction.return_value = _Transaction()
    pool = MagicMock()
    pool.acquire.return_value = _AsyncContext(conn)

    with (
        patch("scholight.db.migrate._MIGRATIONS_DIR", tmp_path),
        patch("scholight.db.migrate.assert_schema_compatible", new_callable=AsyncMock),
        pytest.raises(RuntimeError, match="checksum mismatch"),
    ):
        await run_migrations(pool)


@pytest.mark.asyncio
async def test_unrecognized_migration_filename_fails_before_database_changes(
    tmp_path: Path,
) -> None:
    (tmp_path / "create_example.sql").write_text("SELECT 1;", encoding="utf-8")
    conn = MagicMock()
    conn.execute = AsyncMock()

    with (
        patch("scholight.db.migrate._MIGRATIONS_DIR", tmp_path),
        pytest.raises(ValueError, match="invalid Scholight migration filename"),
    ):
        await apply_migrations(conn)

    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_migration_version_fails_before_database_changes(tmp_path: Path) -> None:
    (tmp_path / "001_create_example.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "001_create_other.sql").write_text("SELECT 2;", encoding="utf-8")
    conn = MagicMock()
    conn.execute = AsyncMock()

    with (
        patch("scholight.db.migrate._MIGRATIONS_DIR", tmp_path),
        pytest.raises(ValueError, match="duplicate Scholight migration version"),
    ):
        await apply_migrations(conn)

    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_reviewed_delegated_actor_contract_migration_is_applied(tmp_path: Path) -> None:
    source = Path(__file__).parents[3] / "migrations/004_allow_delegated_usage_actor.sql"
    migration = tmp_path / source.name
    migration.write_bytes(source.read_bytes())
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.transaction.return_value = _Transaction()

    with patch("scholight.db.migrate._MIGRATIONS_DIR", tmp_path):
        await apply_migrations(conn)

    assert any(
        call.args == (migration.read_text(encoding="utf-8"),)
        for call in conn.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_unreviewed_contract_migration_still_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "004_allow_delegated_usage_actor.sql").write_text(
        "-- scholight: migration-phase=contract\n"
        "ALTER TABLE scholight.usage_events DROP CONSTRAINT another_constraint;",
        encoding="utf-8",
    )
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)

    with (
        patch("scholight.db.migrate._MIGRATIONS_DIR", tmp_path),
        pytest.raises(ValueError, match="destructive migration rejected"),
    ):
        await apply_migrations(conn)


def test_baseline_only_creates_product_tables_in_scholight_schema() -> None:
    migration = Path(__file__).parents[3] / "migrations/001_scholight_baseline.sql"

    sql = migration.read_text(encoding="utf-8")
    normalized = " ".join(sql.split())
    usage_table = normalized.split("CREATE TABLE scholight.usage_events", 1)[1].split(
        "CREATE INDEX usage_events_user_created_idx", 1
    )[0]

    assert "CREATE SCHEMA" not in normalized
    assert "CREATE TABLE scholight.anonymous_daily_search_usage" in normalized
    assert "PRIMARY KEY (quota_date, ip_digest, strength)" in normalized
    assert "CHECK (octet_length(ip_digest) = 32)" in normalized
    assert "CHECK (strength IN ('standard', 'thorough'))" in normalized
    assert "CHECK (used_count >= 0)" in normalized
    assert "CREATE TABLE scholight.access_keys" in normalized
    assert "REFERENCES auth.users(id) ON DELETE CASCADE" in normalized
    assert "key_digest BYTEA NOT NULL UNIQUE" in normalized
    assert "CHECK (octet_length(key_digest) = 32)" in normalized
    assert "plaintext" not in sql.lower()
    assert "WHERE revoked_at IS NULL" in normalized
    assert "CREATE TABLE scholight.usage_events" in normalized
    assert "is_admin BOOLEAN NOT NULL DEFAULT FALSE" in normalized
    assert "CREATE TABLE scholight.admin_audit_events" in normalized
    assert "event_id UUID NOT NULL UNIQUE" in normalized
    assert "actor_type IN ('user', 'cli')" in normalized
    assert "action IN ('quota_overrides_updated', 'admin_granted', 'admin_revoked')" in normalized
    assert "before_state JSONB NOT NULL" in normalized
    assert "after_state JSONB NOT NULL" in normalized
    assert "request_id VARCHAR(128) NOT NULL UNIQUE" in normalized
    assert "quota_units INTEGER NOT NULL" in normalized
    assert "access_key_id UUID" in normalized
    assert "operation" not in usage_table
    assert "query_text" not in usage_table
    assert "abstract" not in usage_table.lower()
    assert "ip_address" not in usage_table.lower()
    assert "CREATE TABLE public." not in normalized
    assert "ALTER TABLE auth." not in normalized


def test_baseline_checksum_is_immutable_after_production_release() -> None:
    migration = Path(__file__).parents[3] / "migrations/001_scholight_baseline.sql"

    checksum = hashlib.sha256(migration.read_bytes()).hexdigest()

    assert checksum == "f6415146424ee607efa87ce854f1fe6bccd9c47923ec1f31f6dfb87a51bc0810"


def test_admin_metrics_migration_is_product_scoped_and_expand_only() -> None:
    migration = Path(__file__).parents[3] / "migrations/003_admin_metrics.sql"

    sql = " ".join(migration.read_text(encoding="utf-8").split()).lower()

    assert "scholight." in sql and "auth." not in sql and "drop " not in sql


def test_delegated_actor_migration_only_replaces_expected_constraints() -> None:
    migration = Path(__file__).parents[3] / "migrations/004_allow_delegated_usage_actor.sql"

    sql = " ".join(migration.read_text(encoding="utf-8").split()).lower()

    assert sql.count("drop constraint") == 2
    assert "drop constraint usage_events_actor_type" in sql
    assert "drop constraint usage_events_key_actor" in sql
    assert "drop table" not in sql
    assert "truncate" not in sql
    assert "delete from" not in sql
    assert "auth." not in sql
    assert "actor_type in ('web', 'access_key', 'delegated')" in sql
    assert "actor_type in ('web', 'delegated')" in sql


def test_delegated_actor_migration_requires_explicit_checksum_approval() -> None:
    migration = Path(__file__).parents[3] / "migrations/004_allow_delegated_usage_actor.sql"
    sql = migration.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="destructive migration rejected"):
        validate_expand_only_sql(sql)


def test_survey_migration_is_product_scoped_and_expand_only() -> None:
    migration = Path(__file__).parents[3] / "migrations/005_survey_jobs.sql"
    raw_sql = migration.read_text(encoding="utf-8")
    sql = " ".join(raw_sql.split()).lower()

    validate_expand_only_sql(raw_sql)
    assert "create table scholight.survey_jobs" in sql
    assert "create table scholight.survey_daily_usage" in sql
    assert "references auth.users(id) on delete cascade" in sql
    assert "reserved_count" in sql and "succeeded_count" in sql
    assert "drop " not in sql
    assert "truncate " not in sql
    assert "delete from" not in sql


def test_survey_quota_override_migration_expands_the_shared_strength_constraint() -> None:
    migration = Path(__file__).parents[3] / "migrations/010_survey_quota_overrides.sql"

    sql = " ".join(migration.read_text(encoding="utf-8").split()).lower()

    assert "strength in ('standard', 'thorough', 'survey')" in sql


def test_survey_aggregate_migration_fails_closed_before_replacing_legacy_table() -> None:
    migration = Path(__file__).parents[3] / "migrations/006_survey_aggregate.sql"
    raw_sql = migration.read_text(encoding="utf-8")
    sql = " ".join(raw_sql.split()).lower()

    with pytest.raises(ValueError, match="destructive migration rejected"):
        validate_expand_only_sql(raw_sql)
    assert sql.index("if exists (select 1 from scholight.survey_jobs") < sql.index(
        "drop table scholight.survey_jobs"
    )
    assert "raise exception" in sql
    assert "create table scholight.surveys" in sql
    assert "create table scholight.survey_drafts" in sql
    assert sql.count("create table scholight.survey_jobs") == 1
    assert "progress_stage" not in sql
    assert "progress_updated_at" not in sql
    assert "references auth.users(id) on delete cascade" in sql
    assert (
        "status in ('drafting', 'queued', 'running', 'archiving', 'succeeded', 'failed', 'cancelled')"
        in sql
    )


def test_survey_reliability_migration_is_product_scoped_and_expand_only() -> None:
    migration = Path(__file__).parents[3] / "migrations/007_survey_reliability.sql"
    raw_sql = migration.read_text(encoding="utf-8")
    sql = " ".join(raw_sql.split()).lower()

    validate_expand_only_sql(raw_sql)
    assert "alter table scholight.surveys" in sql
    assert "request_hash" in sql
    assert "queued_at" in sql and "last_claim_at" in sql
    assert "create table scholight.survey_artifact_cleanup_outbox" in sql
    assert "before delete on scholight.surveys" in sql
    assert "from scholight.survey_jobs" in sql
    assert "auth." not in sql
    assert "drop " not in sql
    assert "truncate " not in sql
    assert "delete from" not in sql


def test_survey_cancellation_migration_only_widens_the_job_contract() -> None:
    migration = Path(__file__).parents[3] / "migrations/008_survey_cancellation.sql"
    raw_sql = migration.read_text(encoding="utf-8")
    sql = " ".join(raw_sql.split()).lower()

    with pytest.raises(ValueError, match="destructive migration rejected"):
        validate_expand_only_sql(raw_sql)
    assert "alter table scholight.survey_jobs" in sql
    assert "add column cancel_requested_at timestamptz" in sql
    assert "terminal_outcome in ('succeeded', 'failed', 'cancelled')" in sql
    assert "drop constraint survey_jobs_terminal_outcome" in sql
    assert "drop table" not in sql
    assert "truncate" not in sql
    assert "delete from" not in sql
    assert "auth." not in sql


@pytest.mark.asyncio
async def test_reviewed_survey_aggregate_contract_migration_is_applied(tmp_path: Path) -> None:
    source = Path(__file__).parents[3] / "migrations/006_survey_aggregate.sql"
    migration = tmp_path / source.name
    migration.write_bytes(source.read_bytes())
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.transaction.return_value = _Transaction()

    with patch("scholight.db.migrate._MIGRATIONS_DIR", tmp_path):
        await apply_migrations(conn)

    assert any(
        call.args == (migration.read_text(encoding="utf-8"),)
        for call in conn.execute.await_args_list
    )
