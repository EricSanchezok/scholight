"""Durable recovery ledger independent of the runnable ingestion queue."""

import datetime as dt

from scholight.config import require_full_runtime, settings
from scholight.db.client import get_pool
from scholight.db.queries_ingestion import enqueue_ingestion_job, get_ingestion_job


async def record_deferred_fulltext(rows: list[tuple[str, int]], date: dt.date) -> None:
    """Record every observed version, including replays after cross-store failure."""
    await get_pool().executemany(
        """
        INSERT INTO scholight.deferred_fulltext
            (arxiv_id, target_version, first_seen_date, last_seen_date)
        VALUES ($1, $2, $3, $3)
        ON CONFLICT (arxiv_id) DO UPDATE SET
            target_version = GREATEST(scholight.deferred_fulltext.target_version, EXCLUDED.target_version),
            first_seen_date = LEAST(scholight.deferred_fulltext.first_seen_date, EXCLUDED.first_seen_date),
            last_seen_date = GREATEST(scholight.deferred_fulltext.last_seen_date, EXCLUDED.last_seen_date),
            updated_at = now()
        """,
        [(arxiv_id, version, date) for arxiv_id, version in rows],
    )


async def resume_deferred_fulltext(*, limit: int, apply: bool) -> dict[str, int | bool]:
    """Submit a bounded batch; retain entries until matching work succeeds."""
    require_full_runtime("Deferred full-text recovery")
    if not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT d.arxiv_id, d.target_version
        FROM scholight.deferred_fulltext d
        LEFT JOIN scholight.ingestion_jobs j USING (arxiv_id)
        WHERE j.arxiv_id IS NULL OR j.target_version < d.target_version
           OR j.status IN ('dead', 'succeeded')
        ORDER BY d.first_seen_date, d.arxiv_id LIMIT $1
        """,
        limit,
    )
    enqueued = 0
    if apply:
        for row in rows:
            arxiv_id, version = str(row["arxiv_id"]), int(row["target_version"])
            job = await get_ingestion_job(arxiv_id)
            if job and job.status == "succeeded" and job.target_version >= version:
                await pool.execute(
                    "DELETE FROM scholight.deferred_fulltext WHERE arxiv_id=$1 AND target_version<=$2",
                    arxiv_id,
                    version,
                )
                continue
            changed = await enqueue_ingestion_job(
                arxiv_id, version, "backfill", max_attempts=settings.ingest_max_attempts
            )
            if job and job.status == "dead" and job.target_version >= version:
                from scholight.db.queries_ingestion import retry_ingestion_job

                changed = await retry_ingestion_job(arxiv_id)
            enqueued += int(changed)
    return {"matched": len(rows), "enqueued": enqueued, "dry_run": not apply}
