"""Real PostgreSQL contracts for the durable ingestion queue.

These tests are destructive by design, so they refuse every database URL
except an explicitly named loopback-only test database.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from unittest.mock import patch

import asyncpg
import pytest
import pytest_asyncio

from scholight.db.client import DBError
from scholight.db.queries_admin_analytics import query_admin_analytics
from scholight.db.queries_admin_operations import query_admin_operations
from scholight.db.queries_ingestion import (
    claim_ingestion_job,
    complete_ingestion_job,
    enqueue_ingestion_job,
    fail_ingestion_job,
    get_ingestion_job,
    initialize_sync_cursor,
    mark_sync_started,
    mark_sync_succeeded,
    retry_ingestion_job,
)
from scholight.db.tests.pg_ingestion_support import (
    isolated_database_url,
    replay_ingestion_migrations,
    reset_ingestion_database,
)

pytestmark = pytest.mark.pg_integration


@pytest_asyncio.fixture
async def ingestion_pool() -> asyncpg.Pool:
    pool = await asyncpg.create_pool(isolated_database_url(), min_size=1, max_size=8)
    await reset_ingestion_database(pool)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_migrations_apply_once_and_replay_without_schema_changes(
    ingestion_pool: asyncpg.Pool,
) -> None:
    await replay_ingestion_migrations(ingestion_pool)
    versions = await ingestion_pool.fetch(
        "SELECT version, name FROM scholight.schema_migrations ORDER BY version"
    )

    assert [(row["version"], row["name"]) for row in versions] == [
        (1, "scholight_baseline"),
        (2, "ingestion_queue"),
        (3, "admin_metrics"),
        (4, "allow_delegated_usage_actor"),
        (5, "survey_jobs"),
        (6, "survey_aggregate"),
        (7, "access_keys_all_tools"),
    ]


@pytest.mark.asyncio
async def test_admin_metrics_migration_adds_transport_and_bounded_query_indexes(
    ingestion_pool: asyncpg.Pool,
) -> None:
    transport = await ingestion_pool.fetchrow(
        """
        SELECT is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'scholight'
          AND table_name = 'usage_events'
          AND column_name = 'transport'
        """
    )
    indexes = await ingestion_pool.fetch(
        """
        SELECT indexname
        FROM pg_indexes
        WHERE schemaname = 'scholight'
          AND indexname IN (
              'usage_events_created_idx',
              'ingestion_jobs_succeeded_idx',
              'user_profiles_created_idx',
              'access_keys_created_idx'
          )
        ORDER BY indexname
        """
    )

    assert transport is not None and len(indexes) == 4


@pytest.mark.asyncio
async def test_operations_metrics_execute_against_postgresql(
    ingestion_pool: asyncpg.Pool,
) -> None:
    with patch(
        "scholight.db.queries_admin_operations.get_pool",
        return_value=ingestion_pool,
    ):
        metrics = await query_admin_operations(days=7, issue_limit=20)

    assert len(metrics["intake"]) == 7


@pytest.mark.asyncio
async def test_analytics_metrics_execute_against_postgresql(
    ingestion_pool: asyncpg.Pool,
) -> None:
    end = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
    with patch(
        "scholight.db.queries_admin_analytics.get_pool",
        return_value=ingestion_pool,
    ):
        metrics = await query_admin_analytics(
            start=end - dt.timedelta(days=30),
            end=end,
        )

    assert metrics["searches"]["total"] == 0


@pytest.mark.asyncio
async def test_enqueue_deduplicates_and_higher_version_reactivates(
    ingestion_pool: asyncpg.Pool,
) -> None:
    with patch("scholight.db.queries_ingestion.get_pool", return_value=ingestion_pool):
        first = await enqueue_ingestion_job("2401.00001", 1, "new", max_attempts=8)
        duplicate = await enqueue_ingestion_job("2401.00001", 1, "new", max_attempts=8)
        claimed = await claim_ingestion_job("worker-old", 7200)
        promoted = await enqueue_ingestion_job("2401.00001", 2, "revision", max_attempts=8)
        job = await get_ingestion_job("2401.00001")

    assert first is True
    assert duplicate is False
    assert claimed is not None
    assert promoted is True
    assert job is not None
    assert (job.target_version, job.status, job.attempt_count, job.lease_owner) == (
        2,
        "pending",
        0,
        None,
    )
    assert (
        await ingestion_pool.fetchval(
            "SELECT count(*) FROM scholight.ingestion_jobs WHERE arxiv_id = '2401.00001'"
        )
        == 1
    )


@pytest.mark.asyncio
async def test_two_concurrent_claims_never_return_the_same_job(
    ingestion_pool: asyncpg.Pool,
) -> None:
    with patch("scholight.db.queries_ingestion.get_pool", return_value=ingestion_pool):
        await enqueue_ingestion_job("2401.00001", 1, "new", max_attempts=8)
        await enqueue_ingestion_job("2401.00002", 1, "new", max_attempts=8)
        jobs = await asyncio.gather(
            claim_ingestion_job("worker-a", 7200),
            claim_ingestion_job("worker-b", 7200),
        )

    assert {job.arxiv_id for job in jobs if job is not None} == {
        "2401.00001",
        "2401.00002",
    }


@pytest.mark.asyncio
async def test_expired_lease_is_recovered_by_another_worker(
    ingestion_pool: asyncpg.Pool,
) -> None:
    with patch("scholight.db.queries_ingestion.get_pool", return_value=ingestion_pool):
        await enqueue_ingestion_job("2401.00001", 1, "new", max_attempts=8)
        first = await claim_ingestion_job("worker-a", 7200)
        await ingestion_pool.execute(
            "UPDATE scholight.ingestion_jobs "
            "SET lease_expires_at = now() - interval '1 second' "
            "WHERE arxiv_id = '2401.00001'"
        )
        recovered = await claim_ingestion_job("worker-b", 7200)

    assert first is not None
    assert recovered is not None
    assert (recovered.lease_owner, recovered.attempt_count) == ("worker-b", 2)


@pytest.mark.asyncio
async def test_retry_dead_success_and_manual_reactivation(
    ingestion_pool: asyncpg.Pool,
) -> None:
    with patch("scholight.db.queries_ingestion.get_pool", return_value=ingestion_pool):
        await enqueue_ingestion_job("2401.00001", 1, "new", max_attempts=8)
        await claim_ingestion_job("worker-a", 7200)
        await fail_ingestion_job(
            "2401.00001",
            "worker-a",
            code="temporary",
            message="retry",
            retry_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
        )
        retry_claim = await claim_ingestion_job("worker-b", 7200)
        await complete_ingestion_job("2401.00001", "worker-b")
        succeeded = await get_ingestion_job("2401.00001")
        reactivated = await retry_ingestion_job("2401.00001")
        manual_claim = await claim_ingestion_job("worker-c", 7200)
        await fail_ingestion_job(
            "2401.00001",
            "worker-c",
            code="deterministic",
            message="dead",
            retry_at=None,
        )
        dead = await get_ingestion_job("2401.00001")
        no_claim = await claim_ingestion_job("worker-d", 7200)

    assert retry_claim is not None
    assert succeeded is not None and succeeded.status == "succeeded"
    assert reactivated is True
    assert manual_claim is not None
    assert dead is not None and dead.status == "dead"
    assert no_claim is None


@pytest.mark.asyncio
async def test_due_retry_is_not_starved_by_older_pending_work(
    ingestion_pool: asyncpg.Pool,
) -> None:
    with patch("scholight.db.queries_ingestion.get_pool", return_value=ingestion_pool):
        await enqueue_ingestion_job("2401.00001", 1, "new", max_attempts=8)
        retry_candidate = await claim_ingestion_job("worker-a", 7200)
        assert retry_candidate is not None
        await fail_ingestion_job(
            retry_candidate.arxiv_id,
            "worker-a",
            code="temporary",
            message="retry",
            retry_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
        )
        await enqueue_ingestion_job("2401.00002", 1, "new", max_attempts=8)
        await ingestion_pool.execute(
            """
            UPDATE scholight.ingestion_jobs
            SET available_at = now() - interval '1 day'
            WHERE arxiv_id = '2401.00002'
            """
        )
        claimed = await claim_ingestion_job("worker-b", 7200)

    assert claimed is not None
    assert claimed.arxiv_id == "2401.00001"


@pytest.mark.asyncio
async def test_sync_cursor_rejects_a_gap_and_preserves_last_success(
    ingestion_pool: asyncpg.Pool,
) -> None:
    with patch("scholight.db.queries_ingestion.get_pool", return_value=ingestion_pool):
        await mark_sync_started("arxiv")
        await initialize_sync_cursor("arxiv", dt.date(2026, 7, 20))
        with pytest.raises(DBError, match="skip"):
            await mark_sync_succeeded("arxiv", dt.date(2026, 7, 22))

    assert await ingestion_pool.fetchval(
        "SELECT last_successful_date FROM scholight.ingestion_sync_state WHERE source = 'arxiv'"
    ) == dt.date(2026, 7, 20)


@pytest.mark.asyncio
async def test_pending_job_survives_application_pool_restart() -> None:
    first_pool = await asyncpg.create_pool(isolated_database_url(), min_size=1, max_size=2)
    await reset_ingestion_database(first_pool)
    with patch("scholight.db.queries_ingestion.get_pool", return_value=first_pool):
        await enqueue_ingestion_job("2401.00001", 1, "new", max_attempts=8)
    await first_pool.close()

    second_pool = await asyncpg.create_pool(isolated_database_url(), min_size=1, max_size=2)
    try:
        with patch("scholight.db.queries_ingestion.get_pool", return_value=second_pool):
            recovered = await claim_ingestion_job("worker-after-restart", 7200)
    finally:
        await second_pool.close()

    assert recovered is not None
    assert recovered.arxiv_id == "2401.00001"
