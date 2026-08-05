"""Low-cardinality capacity projections for Survey worker autoscaling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import asyncpg
import structlog

from scholight.db.client import DBError, get_pool

logger = structlog.get_logger(__name__)
SurveyQueue = Literal["draft", "survey"]


@dataclass(frozen=True, slots=True)
class SurveyCapacitySnapshot:
    queued: int
    running: int
    outstanding: int
    users_at_limit: int
    oldest_queued_at: datetime | None


async def get_survey_capacity_snapshot(
    *,
    queue: SurveyQueue,
    per_user_concurrency: int,
) -> SurveyCapacitySnapshot:
    """Return one aggregate queue snapshot without identifiers or user data."""
    if queue == "draft":
        query = (
            "WITH live_per_user AS (SELECT s.user_id, count(*) AS running "
            "FROM scholight.survey_drafts d JOIN scholight.surveys s ON s.id = d.survey_id "
            "WHERE d.status = 'running' AND d.lease_owner IS NOT NULL "
            "AND d.lease_expires_at > now() GROUP BY s.user_id), "
            "capacity AS (SELECT "
            "count(*) FILTER (WHERE status = 'queued') AS queued, "
            "count(*) FILTER (WHERE status = 'running' AND lease_owner IS NOT NULL "
            "AND lease_expires_at > now()) AS running, "
            "min(queued_at) FILTER (WHERE status = 'queued') AS oldest_queued_at "
            "FROM scholight.survey_drafts) "
            "SELECT queued, running, queued + running AS outstanding, oldest_queued_at, "
            "(SELECT count(*) FROM live_per_user WHERE running >= $1) AS users_at_limit "
            "FROM capacity"
        )
    else:
        query = (
            "WITH live_per_user AS (SELECT s.user_id, count(*) AS running "
            "FROM scholight.survey_jobs j JOIN scholight.surveys s ON s.id = j.survey_id "
            "WHERE j.status IN ('running', 'archiving') AND j.lease_owner IS NOT NULL "
            "AND j.lease_expires_at > now() GROUP BY s.user_id), "
            "capacity AS (SELECT "
            "count(*) FILTER (WHERE status = 'queued' OR (status = 'archiving' "
            "AND (next_archive_at IS NULL OR next_archive_at <= now()) "
            "AND (lease_expires_at IS NULL OR lease_expires_at <= now()))) AS queued, "
            "count(*) FILTER (WHERE status IN ('running', 'archiving') "
            "AND lease_owner IS NOT NULL AND lease_expires_at > now()) AS running, "
            "min(queued_at) FILTER (WHERE status = 'queued' OR (status = 'archiving' "
            "AND (next_archive_at IS NULL OR next_archive_at <= now()) "
            "AND (lease_expires_at IS NULL OR lease_expires_at <= now()))) "
            "AS oldest_queued_at FROM scholight.survey_jobs) "
            "SELECT queued, running, queued + running AS outstanding, oldest_queued_at, "
            "(SELECT count(*) FROM live_per_user WHERE running >= $1) AS users_at_limit "
            "FROM capacity"
        )
    try:
        row = await get_pool().fetchrow(query, per_user_concurrency)
    except asyncpg.PostgresError as exc:
        logger.error("survey_capacity_read_failed", queue=queue, error_type=type(exc).__name__)
        raise DBError("Failed to read Survey queue capacity") from exc
    if row is None:
        raise DBError("Survey queue capacity projection returned no row")
    return SurveyCapacitySnapshot(
        queued=int(row["queued"]),
        running=int(row["running"]),
        outstanding=int(row["outstanding"]),
        users_at_limit=int(row["users_at_limit"]),
        oldest_queued_at=row["oldest_queued_at"],
    )


__all__ = ["SurveyCapacitySnapshot", "SurveyQueue", "get_survey_capacity_snapshot"]
