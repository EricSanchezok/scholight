"""Real destination journal and vector replacement on fixed, isolated services."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import asyncpg
import boto3
from pymilvus import MilvusClient

from scholight.config import settings
from scholight.db.fulltext_install import target_install
from scholight.db.target_ingestion import TargetQueue, register_target
from scholight.db.tests.pg_ingestion_support import isolated_database_url, reset_ingestion_database
from scholight.models.ingestion_target import IngestionTarget


async def main() -> None:
    endpoint = "http://127.0.0.1:7254"
    client = MilvusClient(endpoint)
    identity = IngestionTarget(
        endpoint,
        str(client.describe_collection("arxiv_papers")["collection_id"]),
        str(client.describe_collection("arxiv_chunks")["collection_id"]),
        settings.embedding_model,
        settings.embedding_dim,
    )
    settings.ingestion_target_id = identity.key
    prefix = "s3://scholight-archive-test/install-" + uuid4().hex
    settings.ingest_recovery_uri = prefix
    s3 = boto3.client(
        "s3",
        endpoint_url="http://127.0.0.1:7290",
        aws_access_key_id="archive-test",
        aws_secret_access_key="archive-test-only",
        region_name="us-east-1",
    )
    pool = await asyncpg.create_pool(isolated_database_url(), min_size=1, max_size=2)
    await reset_ingestion_database(pool)
    workspace = Path(settings.data_root) / "fulltext-smoke"
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        with (
            patch("scholight.db.target_ingestion.get_pool", return_value=pool),
            patch("scholight.db.fulltext_install.get_pool", return_value=pool),
            patch("scholight.db.fulltext_install.get_client", return_value=client),
            patch("boto3.client", return_value=s3),
        ):
            await register_target(identity)
            queue = TargetQueue(identity.key)
            await queue.establish_baseline(dt.date(2024, 1, 1), prefix + "/verified.json", "a" * 64)
            paper = "2401.00000"
            client.upsert(
                "arxiv_papers", data=[{"arxiv_id": paper, "version": 2}], partial_update=True
            )
            await queue.enqueue(paper, 2, "revision", max_attempts=8)
            job = await queue.claim("first-worker", 60)
            assert job is not None and job.lease_owner
            chunks = [
                {
                    "chunk_id": f"{paper}::v2::{i}",
                    "arxiv_id": paper,
                    "chunk_idx": i,
                    "content_text": f"Revised chunk {i}",
                    "content_embedding": [0.25, 0.5] + [0.0] * (settings.embedding_dim - 2),
                }
                for i in range(65)
            ]
            original_upsert = client.upsert
            writes = 0

            def fail_second(name: str, **kwargs: Any) -> Any:
                nonlocal writes
                if name == "arxiv_chunks":
                    writes += 1
                    if writes == 2:
                        raise OSError("injected write interruption")
                return original_upsert(name, **kwargs)

            async with target_install(job, workspace) as install:
                await install.prepare(chunks, {"has_markdown": True})
                with patch.object(client, "upsert", side_effect=fail_second):
                    try:
                        await install.apply()
                    except OSError:
                        pass
                    else:
                        raise AssertionError("Expected interrupted install")
            assert client.get(
                "arxiv_chunks", ids=[paper + "::chunk::0"], consistency_level="Strong"
            )
            assert await queue.release(paper, job.lease_owner)
            resumed = await queue.claim("second-worker", 60)
            assert resumed is not None and resumed.lease_owner != job.lease_owner
            async with target_install(resumed, workspace) as install:
                assert await install.exists()
                manifest = await install.apply()
            assert resumed.lease_owner
            await queue.complete(paper, resumed.lease_owner)
            assert not client.get(
                "arxiv_chunks", ids=[paper + "::chunk::0"], consistency_level="Strong"
            )
            assert (
                len(
                    client.get(
                        "arxiv_chunks",
                        ids=[r["chunk_id"] for r in chunks],
                        consistency_level="Strong",
                    )
                )
                == 65
            )
            assert await pool.fetchval("SELECT chunk_count FROM scholight.fulltext_receipts") == 65
            assert (
                await pool.fetchval("SELECT stage FROM scholight.fulltext_installs") == "complete"
            )
            result = {
                "target_id": identity.key,
                "rows_verified": 65,
                "interrupted_resume_verified": True,
                "chunks_sha256": manifest["chunks_sha256"],
            }
            (workspace / "result.json").write_text(json.dumps(result, indent=2))
    finally:
        client.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
