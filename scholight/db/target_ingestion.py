"""Destination-bound queues and receipts; legacy success is never completion evidence."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from scholight.db.client import DBError, get_pool
from scholight.db.queries_ingestion import _SOURCE_PRIORITY, IngestionJob, JobSource, _job
from scholight.models.ingestion_target import IngestionTarget, fulltext_profile_hash


async def register_target(target: IngestionTarget) -> None:
    identity = json.dumps(asdict(target), sort_keys=True)
    row = await get_pool().fetchrow(
        """INSERT INTO scholight.ingestion_targets (target_id, identity) VALUES ($1, $2::jsonb)
        ON CONFLICT (target_id) DO UPDATE SET identity=scholight.ingestion_targets.identity
        WHERE scholight.ingestion_targets.identity=EXCLUDED.identity RETURNING target_id""",
        target.key,
        identity,
    )
    if row is None:
        raise DBError("Destination identity conflicts with its registered binding")


class TargetLeaseLostError(DBError):
    """A superseded worker must stop without changing its replacement job."""


class TargetQueue:
    def __init__(self, target_id: str, *, profile_sha256: str | None = None) -> None:
        self.target_id = target_id
        self.profile = profile_sha256 or fulltext_profile_hash()

    async def sync_source(self) -> str:
        baseline = await get_pool().fetchval(
            "SELECT baseline_date FROM scholight.ingestion_targets WHERE target_id=$1",
            self.target_id,
        )
        if baseline is None:
            raise DBError("Destination baseline has not been verified")
        return "arxiv:" + self.target_id

    async def establish_baseline(self, date: dt.date, manifest: str, sha256: str) -> None:
        if not manifest.startswith("s3://") or len(sha256) != 64:
            raise ValueError("Baseline requires a durable verified manifest and SHA-256")
        async with get_pool().acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """UPDATE scholight.ingestion_targets SET baseline_date=$2, baseline_manifest=$3,
                    baseline_sha256=$4 WHERE target_id=$1 AND (baseline_date IS NULL OR
                    (baseline_date=$2 AND baseline_manifest=$3 AND baseline_sha256=$4)) RETURNING target_id""",
                self.target_id,
                date,
                manifest,
                sha256,
            )
            if row is None:
                raise DBError("Destination baseline is absent or already bound to another manifest")
            await conn.execute(
                """INSERT INTO scholight.ingestion_sync_state(source,last_successful_date)
                VALUES ($1,$2) ON CONFLICT(source) DO NOTHING""",
                "arxiv:" + self.target_id,
                date,
            )

    async def enqueue(
        self, arxiv_id: str, version: int, source: JobSource, *, max_attempts: int
    ) -> bool:
        row = await get_pool().fetchrow(
            """INSERT INTO scholight.target_ingestion_jobs AS j
                (target_id,arxiv_id,target_version,profile_sha256,source,priority,max_attempts)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT(target_id,arxiv_id) DO UPDATE SET
                target_version=EXCLUDED.target_version, profile_sha256=EXCLUDED.profile_sha256,
                source=CASE WHEN EXCLUDED.target_version>j.target_version OR EXCLUDED.priority<j.priority THEN EXCLUDED.source ELSE j.source END,
                priority=LEAST(j.priority,EXCLUDED.priority), max_attempts=GREATEST(j.max_attempts,EXCLUDED.max_attempts),
                status=CASE WHEN EXCLUDED.target_version>j.target_version OR EXCLUDED.profile_sha256<>j.profile_sha256 THEN 'pending' ELSE j.status END,
                attempt_count=CASE WHEN EXCLUDED.target_version>j.target_version OR EXCLUDED.profile_sha256<>j.profile_sha256 THEN 0 ELSE j.attempt_count END,
                available_at=CASE WHEN EXCLUDED.target_version>j.target_version OR EXCLUDED.profile_sha256<>j.profile_sha256 THEN now() ELSE j.available_at END,
                lease_owner=CASE WHEN EXCLUDED.target_version>j.target_version OR EXCLUDED.profile_sha256<>j.profile_sha256 THEN NULL ELSE j.lease_owner END,
                lease_expires_at=CASE WHEN EXCLUDED.target_version>j.target_version OR EXCLUDED.profile_sha256<>j.profile_sha256 THEN NULL ELSE j.lease_expires_at END,
                updated_at=now()
            WHERE EXCLUDED.target_version>=j.target_version AND
                (EXCLUDED.target_version>j.target_version OR EXCLUDED.profile_sha256<>j.profile_sha256 OR EXCLUDED.priority<j.priority)
            RETURNING arxiv_id""",
            self.target_id,
            arxiv_id,
            version,
            self.profile,
            source,
            _SOURCE_PRIORITY[source],
            max_attempts,
        )
        return row is not None

    async def claim(self, worker: str, lease_seconds: int) -> IngestionJob | None:
        async with get_pool().acquire() as conn, conn.transaction():
            counter = await conn.fetchval(
                "SELECT claim_count FROM scholight.ingestion_targets WHERE target_id=$1 FOR UPDATE",
                self.target_id,
            )
            if counter is None:
                raise DBError("Unregistered destination")
            # A killed process cannot record its own failure. Expired final
            # attempts remain visible for investigation rather than looping forever.
            await conn.execute(
                """UPDATE scholight.target_ingestion_jobs SET status='dead',lease_owner=NULL,
                lease_expires_at=NULL,updated_at=now(),last_error_code='lease_expired',
                last_error_message='Unfinished attempts exhausted the retry budget; investigate task termination before retry'
                WHERE target_id=$1 AND profile_sha256=$2 AND status='running'
                  AND lease_expires_at<=now() AND attempt_count>=max_attempts""",
                self.target_id,
                self.profile,
            )
            row = await conn.fetchrow(
                """WITH candidate AS (
                    SELECT arxiv_id FROM scholight.target_ingestion_jobs
                    WHERE target_id=$1 AND profile_sha256=$5 AND (
                        (status IN ('pending','retry') AND available_at<=now()) OR
                        (status='running' AND lease_expires_at<=now()))
                    ORDER BY CASE WHEN $4::boolean AND source='backfill' AND created_at<now()-interval '1 day'
                        THEN 0 ELSE priority END, available_at,created_at,arxiv_id
                    FOR UPDATE SKIP LOCKED LIMIT 1
                ) UPDATE scholight.target_ingestion_jobs j SET status='running',attempt_count=j.attempt_count+1,
                    lease_owner=$2,lease_expires_at=now()+make_interval(secs=>$3),updated_at=now()
                FROM candidate WHERE j.target_id=$1 AND j.arxiv_id=candidate.arxiv_id RETURNING j.*""",
                self.target_id,
                worker + ":" + uuid4().hex,
                lease_seconds,
                (counter + 1) % 5 == 0,
                self.profile,
            )
            if row is not None:
                await conn.execute(
                    "UPDATE scholight.ingestion_targets SET claim_count=claim_count+1 WHERE target_id=$1",
                    self.target_id,
                )
        return _job(row) if row else None

    async def renew(self, arxiv_id: str, owner: str, seconds: int) -> bool:
        result = await get_pool().execute(
            """UPDATE scholight.target_ingestion_jobs SET lease_expires_at=now()+make_interval(secs=>$4),updated_at=now()
            WHERE target_id=$1 AND arxiv_id=$2 AND lease_owner=$3 AND status='running' AND lease_expires_at>now()""",
            self.target_id,
            arxiv_id,
            owner,
            seconds,
        )
        return str(result) != "UPDATE 0"

    async def release(self, arxiv_id: str, owner: str) -> bool:
        result = await get_pool().execute(
            """UPDATE scholight.target_ingestion_jobs SET status='pending',attempt_count=GREATEST(attempt_count-1,0),
            available_at=now(),lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
            WHERE target_id=$1 AND arxiv_id=$2 AND lease_owner=$3 AND status='running' AND lease_expires_at>now()""",
            self.target_id,
            arxiv_id,
            owner,
        )
        return str(result) != "UPDATE 0"

    async def complete(self, arxiv_id: str, owner: str) -> None:
        result = await get_pool().execute(
            """UPDATE scholight.target_ingestion_jobs j SET status='succeeded',succeeded_at=now(),
                lease_owner=NULL,lease_expires_at=NULL,updated_at=now(),last_error_code=NULL,last_error_message=NULL
            WHERE target_id=$1 AND arxiv_id=$2 AND lease_owner=$3 AND status='running' AND lease_expires_at>now()
              AND EXISTS (SELECT 1 FROM scholight.fulltext_receipts r WHERE r.target_id=j.target_id
                AND r.arxiv_id=j.arxiv_id AND r.paper_version=j.target_version AND r.profile_sha256=j.profile_sha256)""",
            self.target_id,
            arxiv_id,
            owner,
        )
        if result == "UPDATE 0":
            raise DBError("Missing verified destination receipt or current job lease")

    async def fail(
        self, arxiv_id: str, owner: str, *, code: str, message: str, retry_at: dt.datetime | None
    ) -> None:
        result = await get_pool().execute(
            """UPDATE scholight.target_ingestion_jobs SET status=$4, available_at=COALESCE($5,available_at),
                last_error_code=$6,last_error_message=$7,lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
            WHERE target_id=$1 AND arxiv_id=$2 AND lease_owner=$3 AND status='running' AND lease_expires_at>now()""",
            self.target_id,
            arxiv_id,
            owner,
            "retry" if retry_at else "dead",
            retry_at,
            code[:64],
            message[:1000],
        )
        if result == "UPDATE 0":
            raise DBError("Ingestion job lease is no longer owned")

    async def get(self, arxiv_id: str) -> IngestionJob | None:
        row = await get_pool().fetchrow(
            "SELECT * FROM scholight.target_ingestion_jobs WHERE target_id=$1 AND arxiv_id=$2",
            self.target_id,
            arxiv_id,
        )
        return _job(row) if row else None

    async def retry(self, arxiv_id: str) -> bool:
        result = await get_pool().execute(
            """UPDATE scholight.target_ingestion_jobs SET status='pending',attempt_count=0,available_at=now(),
                last_error_code=NULL,last_error_message=NULL,updated_at=now()
            WHERE target_id=$1 AND arxiv_id=$2 AND status='dead'""",
            self.target_id,
            arxiv_id,
        )
        return str(result) != "UPDATE 0"

    async def record_scope(self, rows: list[tuple[str, int]], date: dt.date, reason: str) -> None:
        await get_pool().executemany(
            """INSERT INTO scholight.fulltext_scope AS s(target_id,arxiv_id,target_version,reasons,first_seen_date,last_seen_date)
            VALUES($1,$2,$3,ARRAY[$5::text],$4,$4) ON CONFLICT(target_id,arxiv_id) DO UPDATE SET
            target_version=GREATEST(s.target_version,EXCLUDED.target_version),
            first_seen_date=LEAST(s.first_seen_date,EXCLUDED.first_seen_date),last_seen_date=GREATEST(s.last_seen_date,EXCLUDED.last_seen_date),
            reasons=ARRAY(SELECT DISTINCT unnest(s.reasons||EXCLUDED.reasons))""",
            [(self.target_id, paper, version, date, reason) for paper, version in rows],
        )

    async def status(self) -> dict[str, Any]:
        rows = await get_pool().fetch(
            "SELECT status,count(*) AS count FROM scholight.target_ingestion_jobs WHERE target_id=$1 GROUP BY status",
            self.target_id,
        )
        queue = await get_pool().fetchrow(
            """SELECT count(*) FILTER (WHERE status IN ('pending','retry')) AS backlog,
            count(*) FILTER (WHERE status='dead') AS dead,
            COALESCE(EXTRACT(EPOCH FROM now()-min(created_at) FILTER (WHERE status IN ('pending','retry'))),0)::bigint AS oldest_age_seconds
            FROM scholight.target_ingestion_jobs WHERE target_id=$1""",
            self.target_id,
        )
        state = await get_pool().fetchrow(
            "SELECT last_successful_date,last_started_at,last_succeeded_at,last_error_code,last_error_message FROM scholight.ingestion_sync_state WHERE source=$1",
            "arxiv:" + self.target_id,
        )
        return {
            "target_id": self.target_id,
            "jobs": {row["status"]: row["count"] for row in rows},
            "queue": dict(queue) if queue else {},
            "sync": dict(state) if state else None,
        }

    async def resume_scope(self, *, limit: int, apply: bool) -> dict[str, int | bool]:
        """Queue only the reviewed scope; keep its audit rows and terminal failures."""
        if not 1 <= limit <= 10000:
            raise ValueError("limit must be between 1 and 10000")
        rows = await get_pool().fetch(
            """SELECT s.arxiv_id,s.target_version FROM scholight.fulltext_scope s
            LEFT JOIN scholight.target_ingestion_jobs j USING (target_id,arxiv_id)
            WHERE s.target_id=$1 AND (j.arxiv_id IS NULL OR j.target_version<s.target_version
              OR j.profile_sha256<>$2)
              AND NOT EXISTS (SELECT 1 FROM scholight.fulltext_receipts r
                WHERE r.target_id=s.target_id AND r.arxiv_id=s.arxiv_id
                AND r.paper_version=s.target_version AND r.profile_sha256=$2)
            ORDER BY s.first_seen_date,s.arxiv_id LIMIT $3""",
            self.target_id,
            self.profile,
            limit,
        )
        enqueued = 0
        if apply:
            from scholight.config import settings

            for row in rows:
                enqueued += int(
                    await self.enqueue(
                        str(row["arxiv_id"]),
                        int(row["target_version"]),
                        "backfill",
                        max_attempts=settings.ingest_max_attempts,
                    )
                )
        return {"matched": len(rows), "enqueued": enqueued, "dry_run": not apply}
