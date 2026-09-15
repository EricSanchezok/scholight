"""A conflicting completion proof cannot mark an installation complete."""

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest

from scholight.config import settings
from scholight.db.client import DBError
from scholight.db.fulltext_install import target_install
from scholight.db.target_ingestion import TargetQueue, register_target
from scholight.db.tests.pg_ingestion_support import isolated_database_url, reset_ingestion_database
from scholight.db.tests.test_target_ingestion_pg import target

pytestmark = pytest.mark.pg_integration


@pytest.mark.asyncio
async def test_four_installs_keep_heartbeat_connection_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pool = await asyncpg.create_pool(isolated_database_url(), min_size=1, max_size=5)
    try:
        await reset_ingestion_database(pool)
        monkeypatch.setattr("scholight.db.target_ingestion.get_pool", lambda: pool)
        monkeypatch.setattr("scholight.db.fulltext_install.get_pool", lambda: pool)
        monkeypatch.setattr("scholight.db.fulltext_install.get_client", object)
        monkeypatch.setattr(
            "scholight.db.fulltext_install.FulltextInstall",
            lambda *args, **kwargs: SimpleNamespace(**kwargs),
        )
        identity = target()
        monkeypatch.setattr(settings, "ingestion_target_id", identity.key)
        monkeypatch.setattr(settings, "ingest_recovery_uri", "s3://isolated-receipts/recovery")
        await register_target(identity)
        queue = TargetQueue(identity.key)
        for i in range(4):
            await queue.enqueue(f"2609.{i + 1:05d}", 1, "new", max_attempts=8)
        jobs = [await queue.claim(f"worker-{i}", 300) for i in range(4)]
        async with AsyncExitStack() as stack:
            for job in jobs:
                assert job is not None and job.lease_owner is not None
                await stack.enter_async_context(target_install(job, tmp_path / job.arxiv_id))
            async with asyncio.timeout(2):
                for job in jobs:
                    assert job is not None and job.lease_owner is not None
                    assert await queue.renew(job.arxiv_id, job.lease_owner, 300)
        for job in jobs:
            assert job is not None and job.lease_owner is not None
            assert await queue.release(job.arxiv_id, job.lease_owner)
        assert (
            await pool.fetchval(
                "SELECT count(*) FROM scholight.target_ingestion_jobs WHERE status='running'"
            )
            == 0
        )
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_conflicting_receipt_rolls_back_complete_journal_stage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pool = await asyncpg.create_pool(isolated_database_url(), min_size=1, max_size=2)
    try:
        await reset_ingestion_database(pool)
        monkeypatch.setattr("scholight.db.target_ingestion.get_pool", lambda: pool)
        monkeypatch.setattr("scholight.db.fulltext_install.get_pool", lambda: pool)
        monkeypatch.setattr("scholight.db.fulltext_install.get_client", object)
        monkeypatch.setattr(
            "scholight.db.fulltext_install.FulltextInstall",
            lambda *args, **kwargs: SimpleNamespace(**kwargs),
        )
        identity = target()
        monkeypatch.setattr(settings, "ingestion_target_id", identity.key)
        monkeypatch.setattr(settings, "ingest_recovery_uri", "s3://isolated-receipts/recovery")
        await register_target(identity)
        queue = TargetQueue(identity.key)
        await queue.enqueue("2609.00001", 1, "new", max_attempts=8)
        job = await queue.claim("worker", 300)
        assert job is not None
        await pool.execute(
            """INSERT INTO scholight.fulltext_receipts
        (target_id,arxiv_id,paper_version,profile_sha256,configuration,chunk_count,chunks_sha256,recovery_manifest)
        VALUES($1,'2609.00001',1,$2,'{}',1,$3,'old-manifest')""",
            identity.key,
            queue.profile,
            "a" * 64,
        )
        async with target_install(job, tmp_path) as install:
            with pytest.raises(DBError, match="receipt"):
                await install.record("complete", {"chunk_count": 1, "chunks_sha256": "b" * 64})
        assert await pool.fetchval("SELECT count(*) FROM scholight.fulltext_installs") == 0
    finally:
        await pool.close()
