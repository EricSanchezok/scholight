"""Read-only Scholight ingestion metrics for product administrators."""

from __future__ import annotations

from typing import Any

import asyncpg
import structlog

from scholight.db.client import DBError, get_pool

logger = structlog.get_logger(__name__)


async def query_admin_operations(*, days: int, issue_limit: int) -> dict[str, Any]:
    """Return bounded pipeline state without reading another product schema."""
    pool = get_pool()
    try:
        sync_row = await pool.fetchrow(
            """
            SELECT last_successful_date, last_started_at, last_succeeded_at,
                   last_error_code, last_error_message
            FROM scholight.ingestion_sync_state
            WHERE source = 'arxiv'
            """
        )
        queue_row = await pool.fetchrow(
            """
            SELECT
                count(*) FILTER (WHERE status = 'pending')::BIGINT AS pending,
                count(*) FILTER (WHERE status = 'running')::BIGINT AS running,
                count(*) FILTER (WHERE status = 'retry')::BIGINT AS retry,
                count(*) FILTER (WHERE status = 'succeeded')::BIGINT AS succeeded,
                count(*) FILTER (WHERE status = 'dead')::BIGINT AS dead,
                min(available_at) FILTER (
                    WHERE status IN ('pending', 'retry')
                ) AS oldest_waiting_at
            FROM scholight.ingestion_jobs
            """
        )
        intake_rows = await pool.fetch(
            """
            WITH days AS (
                SELECT generate_series(
                    (statement_timestamp() AT TIME ZONE 'UTC')::date
                        - make_interval(days => $1 - 1),
                    (statement_timestamp() AT TIME ZONE 'UTC')::date,
                    interval '1 day'
                )::date AS day
            ),
            discovered AS (
                SELECT (created_at AT TIME ZONE 'UTC')::date AS day,
                       count(*)::BIGINT AS count
                FROM scholight.ingestion_jobs
                WHERE created_at >= (
                    (statement_timestamp() AT TIME ZONE 'UTC')::date
                        - make_interval(days => $1 - 1)
                ) AT TIME ZONE 'UTC'
                GROUP BY day
            ),
            completed AS (
                SELECT (succeeded_at AT TIME ZONE 'UTC')::date AS day,
                       count(*)::BIGINT AS count
                FROM scholight.ingestion_jobs
                WHERE succeeded_at >= (
                    (statement_timestamp() AT TIME ZONE 'UTC')::date
                        - make_interval(days => $1 - 1)
                ) AT TIME ZONE 'UTC'
                GROUP BY day
            )
            SELECT days.day,
                   COALESCE(discovered.count, 0)::BIGINT AS discovered,
                   COALESCE(completed.count, 0)::BIGINT AS full_text_completed
            FROM days
            LEFT JOIN discovered USING (day)
            LEFT JOIN completed USING (day)
            ORDER BY days.day
            """,
            days,
        )
        issue_rows = await pool.fetch(
            """
            SELECT arxiv_id, target_version, source, status, attempt_count,
                   max_attempts, available_at AS next_attempt_at,
                   last_error_code, last_error_message, updated_at
            FROM scholight.ingestion_jobs
            WHERE status IN ('retry', 'dead')
            ORDER BY
                CASE status WHEN 'dead' THEN 0 ELSE 1 END,
                updated_at DESC,
                arxiv_id
            LIMIT $1
            """,
            issue_limit,
        )
    except asyncpg.PostgresError as exc:
        logger.error("admin_operations_query_failed", error_type=type(exc).__name__)
        raise DBError("Failed to query Scholight operations metrics") from exc

    queue = dict(queue_row) if queue_row is not None else {}
    return {
        "sync": dict(sync_row) if sync_row is not None else None,
        "queue": {
            "pending": int(queue.get("pending") or 0),
            "running": int(queue.get("running") or 0),
            "retry": int(queue.get("retry") or 0),
            "succeeded": int(queue.get("succeeded") or 0),
            "dead": int(queue.get("dead") or 0),
            "oldest_waiting_at": queue.get("oldest_waiting_at"),
        },
        "intake": [dict(row) for row in intake_rows],
        "recent_issues": [dict(row) for row in issue_rows],
    }


__all__ = ["query_admin_operations"]
