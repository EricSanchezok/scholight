"""Durable PostgreSQL state for the native paper-ingestion pipeline."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Literal, cast

import asyncpg

from scholight.db.client import DBError, get_pool

JobStatus = Literal["pending", "running", "retry", "succeeded", "dead"]
JobSource = Literal["new", "revision", "reconciliation", "backfill", "manual"]

PRIORITY_NEW = 10
PRIORITY_REVISION = 20
PRIORITY_RECONCILIATION = 50
PRIORITY_BACKFILL = 100

_SOURCE_PRIORITY: dict[JobSource, int] = {
    "new": PRIORITY_NEW,
    "revision": PRIORITY_REVISION,
    "reconciliation": PRIORITY_RECONCILIATION,
    "backfill": PRIORITY_BACKFILL,
    "manual": PRIORITY_NEW,
}


@dataclass(frozen=True, slots=True)
class IngestionJob:
    arxiv_id: str
    target_version: int
    source: JobSource
    priority: int
    status: JobStatus
    attempt_count: int
    max_attempts: int
    lease_owner: str | None = None


@dataclass(frozen=True, slots=True)
class SyncState:
    source: str
    last_successful_date: dt.date | None
    last_error_code: str | None
    last_error_message: str | None


def _job(row: asyncpg.Record) -> IngestionJob:
    return IngestionJob(
        arxiv_id=str(row["arxiv_id"]),
        target_version=int(row["target_version"]),
        source=cast("JobSource", row["source"]),
        priority=int(row["priority"]),
        status=cast("JobStatus", row["status"]),
        attempt_count=int(row["attempt_count"]),
        max_attempts=int(row["max_attempts"]),
        lease_owner=cast("str | None", row["lease_owner"]),
    )


async def enqueue_ingestion_job(
    arxiv_id: str,
    target_version: int,
    source: JobSource,
    *,
    max_attempts: int,
) -> bool:
    """Insert or safely promote one paper job. Return whether state changed."""
    try:
        row = await get_pool().fetchrow(
            """
            INSERT INTO scholight.ingestion_jobs (
                arxiv_id, target_version, source, priority, max_attempts
            )
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (arxiv_id) DO UPDATE SET
                target_version = GREATEST(
                    scholight.ingestion_jobs.target_version,
                    EXCLUDED.target_version
                ),
                source = CASE
                    WHEN EXCLUDED.target_version > scholight.ingestion_jobs.target_version
                    THEN EXCLUDED.source
                    ELSE scholight.ingestion_jobs.source
                END,
                priority = LEAST(scholight.ingestion_jobs.priority, EXCLUDED.priority),
                max_attempts = GREATEST(scholight.ingestion_jobs.max_attempts, EXCLUDED.max_attempts),
                status = CASE
                    WHEN EXCLUDED.target_version > scholight.ingestion_jobs.target_version
                    THEN 'pending'
                    WHEN scholight.ingestion_jobs.status IN ('dead', 'succeeded')
                    THEN scholight.ingestion_jobs.status
                    ELSE scholight.ingestion_jobs.status
                END,
                attempt_count = CASE
                    WHEN EXCLUDED.target_version > scholight.ingestion_jobs.target_version
                    THEN 0
                    ELSE scholight.ingestion_jobs.attempt_count
                END,
                available_at = CASE
                    WHEN EXCLUDED.target_version > scholight.ingestion_jobs.target_version
                    THEN now()
                    ELSE scholight.ingestion_jobs.available_at
                END,
                lease_owner = CASE
                    WHEN EXCLUDED.target_version > scholight.ingestion_jobs.target_version
                    THEN NULL
                    ELSE scholight.ingestion_jobs.lease_owner
                END,
                lease_expires_at = CASE
                    WHEN EXCLUDED.target_version > scholight.ingestion_jobs.target_version
                    THEN NULL
                    ELSE scholight.ingestion_jobs.lease_expires_at
                END,
                last_error_code = CASE
                    WHEN EXCLUDED.target_version > scholight.ingestion_jobs.target_version
                    THEN NULL
                    ELSE scholight.ingestion_jobs.last_error_code
                END,
                last_error_message = CASE
                    WHEN EXCLUDED.target_version > scholight.ingestion_jobs.target_version
                    THEN NULL
                    ELSE scholight.ingestion_jobs.last_error_message
                END,
                updated_at = now()
            WHERE EXCLUDED.target_version >= scholight.ingestion_jobs.target_version
              AND (
                EXCLUDED.target_version > scholight.ingestion_jobs.target_version
                OR EXCLUDED.priority < scholight.ingestion_jobs.priority
              )
            RETURNING arxiv_id
            """,
            arxiv_id,
            target_version,
            source,
            _SOURCE_PRIORITY[source],
            max_attempts,
        )
    except asyncpg.PostgresError as exc:
        raise DBError("Unable to enqueue ingestion job") from exc
    return row is not None


async def get_sync_state(source: str) -> SyncState | None:
    try:
        row = await get_pool().fetchrow(
            """
            SELECT source, last_successful_date, last_error_code, last_error_message
            FROM scholight.ingestion_sync_state
            WHERE source = $1
            """,
            source,
        )
    except asyncpg.PostgresError as exc:
        raise DBError("Unable to read ingestion sync state") from exc
    if row is None:
        return None
    return SyncState(
        source=str(row["source"]),
        last_successful_date=cast("dt.date | None", row["last_successful_date"]),
        last_error_code=cast("str | None", row["last_error_code"]),
        last_error_message=cast("str | None", row["last_error_message"]),
    )


async def mark_sync_started(source: str) -> None:
    try:
        await get_pool().execute(
            """
            INSERT INTO scholight.ingestion_sync_state (source, last_started_at)
            VALUES ($1, now())
            ON CONFLICT (source) DO UPDATE SET
                last_started_at = now(),
                updated_at = now()
            """,
            source,
        )
    except asyncpg.PostgresError as exc:
        raise DBError("Unable to start ingestion sync") from exc


async def mark_sync_succeeded(source: str, synced_date: dt.date) -> None:
    """Advance the cursor by exactly one consecutive date."""
    try:
        result = await get_pool().execute(
            """
            UPDATE scholight.ingestion_sync_state
            SET last_successful_date = $2,
                last_succeeded_at = now(),
                last_error_code = NULL,
                last_error_message = NULL,
                updated_at = now()
            WHERE source = $1
              AND (
                last_successful_date IS NULL
                OR last_successful_date + 1 = $2
                OR last_successful_date = $2
              )
            """,
            source,
            synced_date,
        )
    except asyncpg.PostgresError as exc:
        raise DBError("Unable to advance ingestion sync cursor") from exc
    if result == "UPDATE 0":
        raise DBError("Refusing to skip an ingestion sync date")


async def initialize_sync_cursor(source: str, last_successful_date: dt.date) -> None:
    try:
        await get_pool().execute(
            """
            INSERT INTO scholight.ingestion_sync_state (source, last_successful_date)
            VALUES ($1, $2)
            ON CONFLICT (source) DO UPDATE SET
                last_successful_date = COALESCE(
                    scholight.ingestion_sync_state.last_successful_date,
                    EXCLUDED.last_successful_date
                ),
                updated_at = now()
            """,
            source,
            last_successful_date,
        )
    except asyncpg.PostgresError as exc:
        raise DBError("Unable to initialize ingestion sync cursor") from exc


async def mark_sync_failed(source: str, code: str, message: str) -> None:
    try:
        await get_pool().execute(
            """
            UPDATE scholight.ingestion_sync_state
            SET last_error_code = $2, last_error_message = $3, updated_at = now()
            WHERE source = $1
            """,
            source,
            code[:64],
            message[:1000],
        )
    except asyncpg.PostgresError as exc:
        raise DBError("Unable to record ingestion sync failure") from exc


async def claim_ingestion_job(worker_id: str, lease_seconds: int) -> IngestionJob | None:
    """Atomically recover expired leases and claim one highest-priority job."""
    try:
        row = await get_pool().fetchrow(
            """
            WITH candidate AS (
                SELECT arxiv_id
                FROM scholight.ingestion_jobs
                WHERE (
                    status IN ('pending', 'retry') AND available_at <= now()
                ) OR (
                    status = 'running' AND lease_expires_at <= now()
                )
                ORDER BY
                    priority ASC,
                    CASE WHEN status = 'retry' THEN 0 ELSE 1 END ASC,
                    available_at ASC,
                    created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE scholight.ingestion_jobs AS jobs
            SET status = 'running',
                attempt_count = jobs.attempt_count + 1,
                lease_owner = $1,
                lease_expires_at = now() + make_interval(secs => $2),
                updated_at = now()
            FROM candidate
            WHERE jobs.arxiv_id = candidate.arxiv_id
            RETURNING jobs.*
            """,
            worker_id,
            lease_seconds,
        )
    except asyncpg.PostgresError as exc:
        raise DBError("Unable to claim ingestion job") from exc
    return _job(row) if row is not None else None


async def complete_ingestion_job(arxiv_id: str, worker_id: str) -> None:
    await _finish_job(arxiv_id, worker_id, "succeeded", None, None, None)


async def fail_ingestion_job(
    arxiv_id: str,
    worker_id: str,
    *,
    code: str,
    message: str,
    retry_at: dt.datetime | None,
) -> None:
    status: JobStatus = "retry" if retry_at is not None else "dead"
    await _finish_job(arxiv_id, worker_id, status, code, message, retry_at)


async def _finish_job(
    arxiv_id: str,
    worker_id: str,
    status: JobStatus,
    code: str | None,
    message: str | None,
    retry_at: dt.datetime | None,
) -> None:
    try:
        result = await get_pool().execute(
            """
            UPDATE scholight.ingestion_jobs
            SET status = $3,
                available_at = COALESCE($6, available_at),
                lease_owner = NULL,
                lease_expires_at = NULL,
                last_error_code = $4,
                last_error_message = $5,
                succeeded_at = CASE WHEN $3 = 'succeeded' THEN now() ELSE succeeded_at END,
                updated_at = now()
            WHERE arxiv_id = $1 AND status = 'running' AND lease_owner = $2
            """,
            arxiv_id,
            worker_id,
            status,
            code[:64] if code else None,
            message[:1000] if message else None,
            retry_at,
        )
    except asyncpg.PostgresError as exc:
        raise DBError("Unable to finish ingestion job") from exc
    if result == "UPDATE 0":
        raise DBError("Ingestion job lease is no longer owned by this worker")


async def retry_ingestion_job(arxiv_id: str) -> bool:
    try:
        result = await get_pool().execute(
            """
            UPDATE scholight.ingestion_jobs
            SET status = 'pending', attempt_count = 0, available_at = now(),
                lease_owner = NULL, lease_expires_at = NULL,
                last_error_code = NULL, last_error_message = NULL, updated_at = now()
            WHERE arxiv_id = $1 AND status <> 'running'
            """,
            arxiv_id,
        )
    except asyncpg.PostgresError as exc:
        raise DBError("Unable to retry ingestion job") from exc
    return str(result) != "UPDATE 0"


async def get_ingestion_job(arxiv_id: str) -> IngestionJob | None:
    try:
        row = await get_pool().fetchrow(
            "SELECT * FROM scholight.ingestion_jobs WHERE arxiv_id = $1",
            arxiv_id,
        )
    except asyncpg.PostgresError as exc:
        raise DBError("Unable to read ingestion job") from exc
    return _job(row) if row is not None else None


async def get_ingestion_status() -> dict[str, Any]:
    try:
        rows = await get_pool().fetch(
            """
            SELECT status, count(*)::bigint AS count
            FROM scholight.ingestion_jobs
            GROUP BY status
            """
        )
        state = await get_pool().fetchrow(
            """
            SELECT last_successful_date, last_started_at, last_succeeded_at,
                   last_error_code, last_error_message
            FROM scholight.ingestion_sync_state
            WHERE source = 'arxiv'
            """
        )
    except asyncpg.PostgresError as exc:
        raise DBError("Unable to read ingestion status") from exc
    return {
        "jobs": {str(row["status"]): int(row["count"]) for row in rows},
        "sync": dict(state) if state is not None else None,
    }
