"""Real PostgreSQL coverage for target-scoped administration metrics."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import patch

import asyncpg
import pytest
import pytest_asyncio

from scholight.config import settings
from scholight.db.client import bind_pool_connection
from scholight.db.queries_admin_operations import query_admin_operations
from scholight.db.tests.pg_ingestion_support import isolated_database_url, reset_ingestion_database

pytestmark = pytest.mark.pg_integration

_TARGET = "a" * 64
_OTHER = "b" * 64


@pytest_asyncio.fixture
async def operations_pool(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(isolated_database_url(), min_size=1, max_size=2)
    try:
        await reset_ingestion_database(pool)
        monkeypatch.setattr(settings, "ingestion_target_id", _TARGET)
        await pool.executemany(
            "INSERT INTO scholight.ingestion_targets(target_id,identity) VALUES($1,'{}')",
            [(_TARGET,), (_OTHER,)],
        )
        await pool.executemany(
            """INSERT INTO scholight.ingestion_sync_state
            (source,last_successful_date,last_error_code,last_error_message)
            VALUES($1,$2,$3,$4)""",
            [
                ("arxiv", date(2026, 9, 13), None, None),
                ("arxiv:" + _TARGET, date(2026, 11, 4), "ReadTimeout", "Source timed out"),
                ("arxiv:" + _OTHER, date(2026, 12, 1), None, None),
            ],
        )
        with patch("scholight.db.queries_admin_operations.get_pool", return_value=pool):
            yield pool
    finally:
        await pool.close()


async def _add_job(
    pool: asyncpg.Pool,
    paper: str,
    status: str,
    *,
    target: str = _TARGET,
    created: datetime | None = None,
    succeeded: datetime | None = None,
) -> None:
    now = datetime.now(UTC)
    await pool.execute(
        """INSERT INTO scholight.target_ingestion_jobs
        (target_id,arxiv_id,target_version,profile_sha256,source,priority,status,
         attempt_count,max_attempts,created_at,succeeded_at,available_at,updated_at,
         lease_owner,lease_expires_at,last_error_code,last_error_message)
        VALUES($1,$2,2,$3,'revision',1,$4,3,8,$5,$6,$5,$5,$7,$8,$9,$10)""",
        target,
        paper,
        "c" * 64,
        status,
        created or now,
        succeeded,
        "test-worker" if status == "running" else None,
        now + timedelta(minutes=5) if status == "running" else None,
        "source_or_parse_failure" if status in {"retry", "dead"} else None,
        f"Failed {paper}" if status in {"retry", "dead"} else None,
    )


async def _add_legacy_job(pool: asyncpg.Pool) -> None:
    await pool.execute(
        """INSERT INTO scholight.ingestion_jobs
        (arxiv_id,target_version,source,status,priority,max_attempts,succeeded_at,
         last_error_code,last_error_message)
        VALUES('2601.00001',1,'new','dead',1,8,now(),'legacy_failure','Old failure')"""
    )


@pytest.mark.asyncio
async def test_operations_uses_current_target_cursor_and_error(
    operations_pool: asyncpg.Pool,
) -> None:
    result = await query_admin_operations(days=3, issue_limit=10)
    assert result["sync"] == {
        "last_successful_date": date(2026, 11, 4),
        "last_started_at": None,
        "last_succeeded_at": None,
        "last_error_code": "ReadTimeout",
        "last_error_message": "Source timed out",
    }


@pytest.mark.asyncio
async def test_operations_counts_only_configured_target(operations_pool: asyncpg.Pool) -> None:
    await _add_legacy_job(operations_pool)
    for index, status in enumerate(("pending", "running", "retry", "succeeded", "dead")):
        await _add_job(operations_pool, f"2611.{index:05d}", status)
        await _add_job(operations_pool, f"2611.{index:05d}", "dead", target=_OTHER)
    result = await query_admin_operations(days=3, issue_limit=10)
    assert {
        key: result["queue"][key] for key in ("pending", "running", "retry", "succeeded", "dead")
    } == dict.fromkeys(("pending", "running", "retry", "succeeded", "dead"), 1)


@pytest.mark.asyncio
async def test_operations_intake_filters_both_series_and_fills_utc_days(
    operations_pool: asyncpg.Pool,
) -> None:
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    await _add_legacy_job(operations_pool)
    await _add_job(operations_pool, "2611.00001", "succeeded", created=yesterday, succeeded=today)
    await _add_job(operations_pool, "2611.00002", "retry", created=today)
    await _add_job(
        operations_pool, "2611.00003", "succeeded", target=_OTHER, created=today, succeeded=today
    )
    await _add_job(
        operations_pool,
        "2609.00001",
        "succeeded",
        created=today - timedelta(days=10),
        succeeded=today - timedelta(days=9),
    )
    result = await query_admin_operations(days=3, issue_limit=10)
    assert result["intake"] == [
        {"day": (today - timedelta(days=2)).date(), "discovered": 0, "full_text_completed": 0},
        {"day": yesterday.date(), "discovered": 1, "full_text_completed": 0},
        {"day": today.date(), "discovered": 1, "full_text_completed": 1},
    ]


@pytest.mark.asyncio
async def test_operations_issues_keep_current_target_versions_errors_and_limit(
    operations_pool: asyncpg.Pool,
) -> None:
    await _add_legacy_job(operations_pool)
    await _add_job(operations_pool, "2611.00001", "dead")
    await _add_job(operations_pool, "2611.00002", "retry")
    await _add_job(operations_pool, "2611.99999", "dead", target=_OTHER)
    result = await query_admin_operations(days=3, issue_limit=1)
    assert [
        (r["arxiv_id"], r["target_version"], r["status"], r["last_error_message"])
        for r in result["recent_issues"]
    ] == [("2611.00001", 2, "dead", "Failed 2611.00001")]


@pytest.mark.asyncio
async def test_operations_empty_target_never_falls_back_to_legacy(
    operations_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _add_legacy_job(operations_pool)
    monkeypatch.setattr(settings, "ingestion_target_id", "d" * 64)
    result = await query_admin_operations(days=3, issue_limit=10)
    assert (
        result["sync"],
        result["queue"],
        result["recent_issues"],
        [(p["discovered"], p["full_text_completed"]) for p in result["intake"]],
    ) == (
        None,
        dict.fromkeys(("pending", "running", "retry", "succeeded", "dead"), 0)
        | {"oldest_waiting_at": None},
        [],
        [(0, 0)] * 3,
    )


@pytest.mark.asyncio
async def test_operations_legacy_works_without_target_queue_table(
    operations_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _add_legacy_job(operations_pool)
    monkeypatch.setattr(settings, "ingestion_target_id", "")
    await operations_pool.execute("DROP TABLE scholight.target_ingestion_jobs")
    result = await query_admin_operations(days=3, issue_limit=10)
    assert (
        result["sync"]["last_successful_date"],
        result["queue"]["dead"],
        result["recent_issues"][0]["arxiv_id"],
        result["intake"][-1]["discovered"],
        result["intake"][-1]["full_text_completed"],
    ) == (date(2026, 9, 13), 1, "2601.00001", 1, 1)


@pytest.mark.asyncio
async def test_operations_queries_run_in_a_read_only_transaction(
    operations_pool: asyncpg.Pool,
) -> None:
    await _add_job(operations_pool, "2611.00001", "pending")
    async with bind_pool_connection(operations_pool) as conn, conn.transaction(readonly=True):
        from scholight.db.client import get_pool

        with patch("scholight.db.queries_admin_operations.get_pool", get_pool):
            result: dict[str, Any] = await query_admin_operations(days=3, issue_limit=10)
    assert result["queue"]["pending"] == 1
