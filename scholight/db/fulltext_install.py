"""Serialize per-paper materialization and persist completion only under a live lease."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from scholight.config import settings
from scholight.db.client import DBError, get_pool
from scholight.db.queries_ingestion import IngestionJob
from scholight.db.target_ingestion import TargetLeaseLostError
from scholight.models.ingestion_target import (
    digest_json,
    fulltext_configuration,
    fulltext_profile_hash,
)
from scholight.store.client import get_client
from scholight.store.fulltext_install import FulltextInstall


@asynccontextmanager
async def target_install(
    job: IngestionJob, workspace: Path, *, checkpoint: Callable[[], None] | None = None
) -> AsyncIterator[FulltextInstall]:
    target = settings.ingestion_target_id
    if not target or not settings.ingest_recovery_uri.startswith("s3://") or not job.lease_owner:
        raise DBError(
            "Target fulltext installation requires an S3 recovery prefix and a claimed lease"
        )
    profile = fulltext_profile_hash()
    location = f"{settings.ingest_recovery_uri.rstrip('/')}/{target}/{job.arxiv_id.replace('/', '_')}/v{job.target_version}/{profile}"
    lock = int.from_bytes(
        hashlib.sha256((target + ":" + job.arxiv_id).encode()).digest()[:8], signed=True
    )
    async with get_pool().acquire() as conn:
        acquired = await conn.fetchval("SELECT pg_try_advisory_lock($1)", lock)
        if not acquired:
            raise DBError("Another worker is still installing this paper")
        try:

            async def guard() -> None:
                if checkpoint is not None:
                    checkpoint()
                owned = await conn.fetchval(
                    """SELECT 1 FROM scholight.target_ingestion_jobs WHERE target_id=$1 AND arxiv_id=$2
                    AND target_version=$3 AND profile_sha256=$4 AND lease_owner=$5
                    AND status='running' AND lease_expires_at>now()""",
                    target,
                    job.arxiv_id,
                    job.target_version,
                    profile,
                    job.lease_owner,
                )
                if not owned:
                    raise TargetLeaseLostError(
                        "Fulltext installation no longer owns the current lease"
                    )

            async def record(stage: str, manifest: dict[str, object]) -> None:
                await guard()
                manifest_sha = digest_json(manifest)
                row = await conn.fetchrow(
                    """INSERT INTO scholight.fulltext_installs AS i
                    (target_id,arxiv_id,paper_version,profile_sha256,recovery_manifest,manifest_sha256,stage)
                    VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(target_id,arxiv_id,paper_version,profile_sha256)
                    DO UPDATE SET stage=EXCLUDED.stage,updated_at=now()
                    WHERE i.manifest_sha256=EXCLUDED.manifest_sha256 AND i.recovery_manifest=EXCLUDED.recovery_manifest
                    RETURNING target_id""",
                    target,
                    job.arxiv_id,
                    job.target_version,
                    profile,
                    location + "/manifest.json",
                    manifest_sha,
                    stage,
                )
                if row is None:
                    raise DBError("Fulltext recovery manifest differs from its recorded checksum")
                if stage == "complete":
                    await conn.execute(
                        """INSERT INTO scholight.fulltext_receipts AS r
                        (target_id,arxiv_id,paper_version,profile_sha256,configuration,chunk_count,chunks_sha256,recovery_manifest)
                        VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$8)
                        ON CONFLICT(target_id,arxiv_id,paper_version,profile_sha256) DO UPDATE
                        SET verified_at=now() WHERE r.chunks_sha256=EXCLUDED.chunks_sha256""",
                        target,
                        job.arxiv_id,
                        job.target_version,
                        profile,
                        json.dumps(fulltext_configuration()),
                        manifest["chunk_count"],
                        manifest["chunks_sha256"],
                        location + "/manifest.json",
                    )

            await guard()
            yield FulltextInstall(
                get_client(),
                target_id=target,
                arxiv_id=job.arxiv_id,
                version=job.target_version,
                profile=profile,
                dimension=settings.embedding_dim,
                location=location,
                workspace=workspace,
                guard=guard,
                record=record,
            )
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", lock)
