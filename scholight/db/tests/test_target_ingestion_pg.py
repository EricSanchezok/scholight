"""Target-bound full-text work cannot inherit success from a different collection."""

import datetime as dt
from collections.abc import AsyncIterator
from unittest.mock import patch

import asyncpg
import pytest
import pytest_asyncio

from scholight.db.client import DBError
from scholight.db.queries_ingestion import enqueue_ingestion_job
from scholight.db.target_ingestion import TargetQueue, register_target
from scholight.db.tests.pg_ingestion_support import isolated_database_url, reset_ingestion_database
from scholight.models.ingestion_target import IngestionTarget

pytestmark = pytest.mark.pg_integration


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(isolated_database_url(), min_size=1, max_size=4)
    await reset_ingestion_database(pool)
    try:
        with patch("scholight.db.target_ingestion.get_pool", return_value=pool):
            yield pool
    finally:
        await pool.close()


def target(identity: str = "papers-1") -> IngestionTarget:
    return IngestionTarget(
        endpoint="https://des.invalid",
        papers_id=identity,
        chunks_id="chunks-1",
        embedding_model="Qwen/test",
        embedding_dim=4,
    )


@pytest.mark.asyncio
async def test_old_success_does_not_skip_new_destination(pool: asyncpg.Pool) -> None:
    with patch("scholight.db.queries_ingestion.get_pool", return_value=pool):
        await enqueue_ingestion_job("2608.00001", 1, "new", max_attempts=8)
    await pool.execute("UPDATE scholight.ingestion_jobs SET status='succeeded'")
    t = target()
    await register_target(t)
    queue = TargetQueue(t.key)
    assert await queue.enqueue("2608.00001", 1, "backfill", max_attempts=8)
    job = await queue.claim("worker", 60)
    assert job is not None and job.target_version == 1
    assert await pool.fetchval("SELECT status FROM scholight.ingestion_jobs") == "succeeded"


@pytest.mark.asyncio
async def test_same_names_different_collection_identity_have_separate_jobs(
    pool: asyncpg.Pool,
) -> None:
    first, second = target(), target("papers-2")
    for t in (first, second):
        await register_target(t)
    queue = TargetQueue(first.key)
    await queue.enqueue("2608.00001", 1, "new", max_attempts=8)
    assert await TargetQueue(second.key).claim("worker", 60) is None


@pytest.mark.asyncio
async def test_replay_after_enqueue_failure_heals_v1_and_deduplicates(pool: asyncpg.Pool) -> None:
    t = target()
    await register_target(t)
    queue = TargetQueue(t.key)
    assert await queue.enqueue("2608.00001", 1, "new", max_attempts=8)
    assert not await queue.enqueue("2608.00001", 1, "new", max_attempts=8)
    assert await pool.fetchval("SELECT count(*) FROM scholight.target_ingestion_jobs") == 1


@pytest.mark.asyncio
async def test_new_revision_fences_old_lease_and_never_downgrades(pool: asyncpg.Pool) -> None:
    t = target()
    await register_target(t)
    queue = TargetQueue(t.key)
    await queue.enqueue("2608.00001", 1, "new", max_attempts=8)
    job = await queue.claim("worker", 60)
    assert job is not None and job.lease_owner is not None
    await queue.enqueue("2608.00001", 2, "revision", max_attempts=8)
    assert not await queue.renew(job.arxiv_id, job.lease_owner, 60)
    assert not await queue.enqueue("2608.00001", 1, "new", max_attempts=8)
    job = await queue.claim("worker", 60)
    assert job is not None and job.target_version == 2


@pytest.mark.asyncio
async def test_completion_requires_a_verified_target_receipt(pool: asyncpg.Pool) -> None:
    t = target()
    await register_target(t)
    queue = TargetQueue(t.key)
    await queue.enqueue("2608.00001", 1, "new", max_attempts=8)
    job = await queue.claim("worker", 60)
    assert job is not None and job.lease_owner is not None
    with pytest.raises(DBError, match="receipt"):
        await queue.complete(job.arxiv_id, job.lease_owner)


@pytest.mark.asyncio
async def test_one_in_five_claims_services_history_waiting_a_day(pool: asyncpg.Pool) -> None:
    t = target()
    await register_target(t)
    queue = TargetQueue(t.key)
    await queue.enqueue("2608.00001", 1, "backfill", max_attempts=8)
    await pool.execute(
        "UPDATE scholight.target_ingestion_jobs SET created_at=now()-interval '2 days'"
    )
    for i in range(2, 12):
        await queue.enqueue(f"2608.{i:05d}", 1, "new", max_attempts=8)
    jobs = [await queue.claim(f"worker-{i}", 60) for i in range(5)]
    assert [job.source for job in jobs if job is not None] == ["new"] * 4 + ["backfill"]


@pytest.mark.asyncio
async def test_expired_lease_cannot_renew_or_finish(pool: asyncpg.Pool) -> None:
    t = target()
    await register_target(t)
    queue = TargetQueue(t.key)
    await queue.enqueue("2608.00001", 1, "new", max_attempts=8)
    job = await queue.claim("worker", 60)
    assert job is not None and job.lease_owner is not None
    await pool.execute(
        "UPDATE scholight.target_ingestion_jobs SET lease_expires_at=now()-interval '1 second'"
    )
    assert not await queue.renew(job.arxiv_id, job.lease_owner, 60)
    resumed = await queue.claim("worker", 60)
    assert resumed is not None
    assert resumed.lease_owner != job.lease_owner
    assert not await queue.release(job.arxiv_id, job.lease_owner)


@pytest.mark.asyncio
async def test_destination_cursor_requires_verified_reconciliation(pool: asyncpg.Pool) -> None:
    t = target()
    await register_target(t)
    queue = TargetQueue(t.key)
    with pytest.raises(DBError, match="baseline"):
        await queue.sync_source()
    await queue.establish_baseline(dt.date(2026, 9, 12), "s3://test/verified.json", "a" * 64)
    assert await queue.sync_source() == "arxiv:" + t.key


@pytest.mark.asyncio
async def test_scope_resume_never_skips_due_to_old_success_or_retries_dead_implicitly(
    pool: asyncpg.Pool,
) -> None:
    t = target()
    await register_target(t)
    queue = TargetQueue(t.key)
    await queue.record_scope([("2608.00001", 2)], dt.date(2026, 9, 1), "lean")
    result = await queue.resume_scope(limit=10, apply=False)
    assert result["matched"] == 1 and result["enqueued"] == 0
    assert (await queue.resume_scope(limit=10, apply=True))["enqueued"] == 1
    assert (await queue.resume_scope(limit=10, apply=True))["matched"] == 0
    await pool.execute("UPDATE scholight.target_ingestion_jobs SET status='dead'")
    assert (await queue.resume_scope(limit=10, apply=True))["matched"] == 0
    assert await pool.fetchval("SELECT count(*) FROM scholight.fulltext_scope") == 1


@pytest.mark.asyncio
async def test_target_status_does_not_report_legacy_queue(pool: asyncpg.Pool) -> None:
    t = target()
    await register_target(t)
    queue = TargetQueue(t.key)
    await queue.enqueue("2608.00001", 1, "new", max_attempts=8)
    status = await queue.status()
    assert status["queue"]["backlog"] == 1 and status["sync"] is None
