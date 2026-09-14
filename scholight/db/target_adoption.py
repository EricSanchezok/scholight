"""Adopt only a verified destination baseline and the reviewed interruption scope."""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from scholight.config import settings
from scholight.db.client import DBError, get_pool
from scholight.db.ingestion import _actual_target
from scholight.db.target_ingestion import TargetQueue, register_target
from scholight.models.ingestion_target import digest_json


async def seed_scope(queue: TargetQueue, start: dt.date, end: dt.date) -> int:
    if start > end:
        raise ValueError("Recovery scope start must not follow the baseline date")
    pool = get_pool()
    # Product-owned durable ledgers define the scope; no corpus-wide has_chunks scan.
    await pool.execute(
        """INSERT INTO scholight.fulltext_scope AS s
        (target_id,arxiv_id,target_version,reasons,first_seen_date,last_seen_date)
        SELECT $1,arxiv_id,target_version,ARRAY['deferred'],first_seen_date,last_seen_date
        FROM scholight.deferred_fulltext WHERE last_seen_date BETWEEN $2 AND $3
        ON CONFLICT(target_id,arxiv_id) DO UPDATE SET target_version=GREATEST(s.target_version,EXCLUDED.target_version),
        reasons=ARRAY(SELECT DISTINCT unnest(s.reasons||EXCLUDED.reasons)),
        first_seen_date=LEAST(s.first_seen_date,EXCLUDED.first_seen_date),last_seen_date=GREATEST(s.last_seen_date,EXCLUDED.last_seen_date)""",
        queue.target_id,
        start,
        end,
    )
    await pool.execute(
        """INSERT INTO scholight.fulltext_scope AS s
        (target_id,arxiv_id,target_version,reasons,first_seen_date,last_seen_date)
        SELECT $1,arxiv_id,target_version,ARRAY['interrupted_job'],(updated_at AT TIME ZONE 'UTC')::date,(updated_at AT TIME ZONE 'UTC')::date
        FROM scholight.ingestion_jobs WHERE status IN ('pending','running','retry','dead')
          AND (updated_at AT TIME ZONE 'UTC')::date BETWEEN $2 AND $3
        ON CONFLICT(target_id,arxiv_id) DO UPDATE SET target_version=GREATEST(s.target_version,EXCLUDED.target_version),
        reasons=ARRAY(SELECT DISTINCT unnest(s.reasons||EXCLUDED.reasons)),
        first_seen_date=LEAST(s.first_seen_date,EXCLUDED.first_seen_date),last_seen_date=GREATEST(s.last_seen_date,EXCLUDED.last_seen_date)""",
        queue.target_id,
        start,
        end,
    )
    return int(
        await pool.fetchval(
            "SELECT count(*) FROM scholight.fulltext_scope WHERE target_id=$1", queue.target_id
        )
    )


async def adopt_baseline(
    plan_uri: str,
    proof_sha256: str,
    *,
    date: dt.date,
    scope_start: dt.date,
    scope_end: dt.date | None = None,
) -> dict[str, Any]:
    from scholight.store.archive_io import ArchiveLocation
    from scholight.store.reconcile_inventory import read_rows, validate_delta

    if not plan_uri.startswith("s3://") or len(proof_sha256) != 64:
        raise ValueError("Adoption requires the reviewed S3 verification checksum")
    location = ArchiveLocation(plan_uri)
    proof = await asyncio.to_thread(location.read_json, "verification.json")
    if (
        proof.get("format") != "scholight.abstract-verification.v1"
        or not proof.get("complete")
        or digest_json(proof) != proof_sha256
    ):
        raise ValueError("Missing or changed abstract verification proof")
    plan = await asyncio.to_thread(location.read_json, "manifest.json")
    if digest_json(plan) != proof["plan_sha256"]:
        raise ValueError("Verification references a different reconciliation plan")
    target = await asyncio.to_thread(_actual_target)
    if (
        proof["binding"]["target"] != {"uri": target.endpoint, "collection_id": target.papers_id}
        or proof["model"] != target.embedding_model
        or proof["dimension"] != target.embedding_dim
    ):
        raise ValueError("Actual destination or embedding model differs from verified abstracts")
    if settings.ingestion_target_id and settings.ingestion_target_id != target.key:
        raise ValueError("Configured ingestion target differs from the adoption target")
    cursor = await get_pool().fetchval(
        "SELECT last_successful_date FROM scholight.ingestion_sync_state WHERE source='arxiv'"
    )
    if cursor != date:
        raise DBError("Source PostgreSQL cursor differs from the reviewed baseline date")
    end = scope_end or date
    if scope_start > date or end < date or end > dt.datetime.now(dt.UTC).date():
        raise ValueError("Recovery scope cannot begin after the baseline")
    workspace = Path(settings.data_root) / "baseline-adoption"
    workspace.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="adopt-", dir=workspace) as directory:
        work = Path(directory)
        await asyncio.to_thread(validate_delta, location, plan, work)
        await register_target(target)
        queue = TargetQueue(target.key)
        await seed_scope(queue, scope_start, end)
        batch = []
        for row in read_rows(location, plan["shards"], work):
            batch.append((str(row["arxiv_id"]), int(row["source_version"])))
            if len(batch) == 512:
                await queue.record_scope(batch, date, "abstract_delta")
                batch = []
        if batch:
            await queue.record_scope(batch, date, "abstract_delta")
        # Cursor adoption follows complete scope persistence. Runnable consumers
        # are still paused by the release controller during this operation.
        await queue.establish_baseline(
            date, plan_uri.rstrip("/") + "/verification.json", proof_sha256
        )
        while True:
            result = await queue.resume_scope(limit=10000, apply=True)
            if not result["matched"]:
                break
    return await queue.status()
