"""Content-free usage-event persistence and analytics queries."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import asyncpg
import structlog
from pydantic import BaseModel, ConfigDict, Field

from scholight.db.client import DBError, get_pool

logger = structlog.get_logger(__name__)


class UsageEvent(BaseModel):
    """One authenticated search execution without query or result content."""

    model_config = ConfigDict(frozen=True)

    request_id: str = Field(min_length=1, max_length=128)
    user_id: int
    operation: Literal["search_level1", "search_level2"]
    strength: Literal["standard", "thorough"]
    actor_type: Literal["web", "access_key"]
    access_key_id: UUID | None
    outcome: Literal["success", "degraded", "failed"]
    quota_units: int = Field(ge=0)
    result_count: int | None = Field(default=None, ge=0)
    search_duration_ms: float | None = Field(default=None, ge=0)
    status_code: int | None = None
    error_code: str | None = None


async def insert_usage_event(event: UsageEvent) -> bool:
    """Insert once by request id; retries are harmless."""
    try:
        result = await get_pool().execute(
            "INSERT INTO public.usage_events "
            "(request_id, user_id, operation, strength, actor_type, access_key_id, outcome, "
            "quota_units, result_count, search_duration_ms, status_code, error_code) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12) "
            "ON CONFLICT (request_id) DO NOTHING",
            event.request_id,
            event.user_id,
            event.operation,
            event.strength,
            event.actor_type,
            event.access_key_id,
            event.outcome,
            event.quota_units,
            event.result_count,
            event.search_duration_ms,
            event.status_code,
            event.error_code,
        )
    except asyncpg.PostgresError as exc:
        logger.error("usage_event_insert_failed", error_type=type(exc).__name__)
        raise DBError("Failed to write usage event") from exc
    return str(result) == "INSERT 0 1"


async def query_usage_summary(user_id: int) -> dict[str, Any]:
    """Aggregate the current natural UTC month for one owner."""
    try:
        row = await get_pool().fetchrow(
            "SELECT "
            "COALESCE(sum(quota_units), 0)::BIGINT AS searches_this_month, "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY search_duration_ms) "
            "FILTER (WHERE outcome IN ('success', 'degraded') "
            "AND search_duration_ms IS NOT NULL) AS typical_response_ms, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY search_duration_ms) "
            "FILTER (WHERE outcome IN ('success', 'degraded') "
            "AND search_duration_ms IS NOT NULL) AS p95_response_ms, "
            "count(*) FILTER (WHERE outcome = 'success')::BIGINT AS success_count, "
            "count(*) FILTER (WHERE outcome = 'degraded')::BIGINT AS degraded_count, "
            "count(*) FILTER (WHERE outcome = 'failed')::BIGINT AS failed_count "
            "FROM public.usage_events WHERE user_id = $1 "
            "AND created_at >= date_trunc('month', now())",
            user_id,
        )
    except asyncpg.PostgresError as exc:
        logger.error("usage_summary_query_failed", error_type=type(exc).__name__)
        raise DBError("Failed to query usage summary") from exc
    return dict(row)


async def query_volume(
    user_id: int,
    *,
    start: datetime,
    end: datetime,
    access_key_id: UUID | None,
) -> list[dict[str, Any]]:
    try:
        rows = await get_pool().fetch(
            "SELECT date_trunc('day', created_at) AS bucket_start, "
            "COALESCE(sum(quota_units) FILTER (WHERE strength = 'standard'), 0)::BIGINT "
            "AS standard, "
            "COALESCE(sum(quota_units) FILTER (WHERE strength = 'thorough'), 0)::BIGINT "
            "AS thorough "
            "FROM public.usage_events WHERE user_id = $1 "
            "AND created_at >= $2 AND created_at < $3 "
            "AND ($4::UUID IS NULL OR access_key_id = $4) "
            "GROUP BY bucket_start ORDER BY bucket_start",
            user_id,
            start,
            end,
            access_key_id,
        )
    except asyncpg.PostgresError as exc:
        logger.error("usage_volume_query_failed", error_type=type(exc).__name__)
        raise DBError("Failed to query usage volume") from exc
    return [dict(row) for row in rows]


async def query_latency(
    user_id: int,
    *,
    start: datetime,
    end: datetime,
    access_key_id: UUID | None,
) -> list[dict[str, Any]]:
    try:
        async with get_pool().acquire() as connection:
            rows = await connection.fetch(
                "SELECT date_trunc('day', created_at) AS bucket_start, "
                "percentile_cont(0.5) WITHIN GROUP (ORDER BY search_duration_ms) "
                "FILTER (WHERE strength = 'standard') AS standard_p50_ms, "
                "percentile_cont(0.5) WITHIN GROUP (ORDER BY search_duration_ms) "
                "FILTER (WHERE strength = 'thorough') AS thorough_p50_ms, "
                "percentile_cont(0.95) WITHIN GROUP (ORDER BY search_duration_ms) "
                "AS overall_p95_ms, count(*)::BIGINT AS sample_count "
                "FROM public.usage_events WHERE user_id = $1 "
                "AND created_at >= $2 AND created_at < $3 "
                "AND ($4::UUID IS NULL OR access_key_id = $4) "
                "AND outcome IN ('success', 'degraded') "
                "AND search_duration_ms IS NOT NULL "
                "GROUP BY bucket_start ORDER BY bucket_start",
                user_id,
                start,
                end,
                access_key_id,
            )
    except asyncpg.PostgresError as exc:
        logger.error("usage_latency_query_failed", error_type=type(exc).__name__)
        raise DBError("Failed to query usage latency") from exc
    return [dict(row) for row in rows]


def _records_sql(*, include_cursor: bool) -> str:
    if include_cursor:
        return (
            "SELECT u.id, u.created_at, u.actor_type, u.access_key_id, "
            "k.name AS access_key_name, k.key_last4 AS access_key_last4, "
            "u.strength, u.search_duration_ms, u.result_count, u.outcome, "
            "u.quota_units, u.status_code, u.error_code "
            "FROM public.usage_events u LEFT JOIN public.access_keys k "
            "ON k.id = u.access_key_id AND k.user_id = u.user_id "
            "WHERE u.user_id = $1 AND u.created_at >= $2 AND u.created_at < $3 "
            "AND ($4::VARCHAR IS NULL OR u.strength = $4) "
            "AND ($5::VARCHAR IS NULL OR u.actor_type = $5) "
            "AND ($6::UUID IS NULL OR u.access_key_id = $6) "
            "AND ($7::VARCHAR IS NULL OR u.outcome = $7) "
            "AND (u.created_at, u.id) < ($9, $10) "
            "ORDER BY u.created_at DESC, u.id DESC LIMIT $8"
        )
    return (
        "SELECT u.id, u.created_at, u.actor_type, u.access_key_id, "
        "k.name AS access_key_name, k.key_last4 AS access_key_last4, "
        "u.strength, u.search_duration_ms, u.result_count, u.outcome, "
        "u.quota_units, u.status_code, u.error_code "
        "FROM public.usage_events u LEFT JOIN public.access_keys k "
        "ON k.id = u.access_key_id AND k.user_id = u.user_id "
        "WHERE u.user_id = $1 AND u.created_at >= $2 AND u.created_at < $3 "
        "AND ($4::VARCHAR IS NULL OR u.strength = $4) "
        "AND ($5::VARCHAR IS NULL OR u.actor_type = $5) "
        "AND ($6::UUID IS NULL OR u.access_key_id = $6) "
        "AND ($7::VARCHAR IS NULL OR u.outcome = $7) "
        "ORDER BY u.created_at DESC, u.id DESC LIMIT $8"
    )


async def query_usage_records(
    user_id: int,
    *,
    start: datetime,
    end: datetime,
    limit: int,
    strength: str | None,
    actor_type: str | None,
    access_key_id: UUID | None,
    outcome: str | None,
    cursor: tuple[datetime, int] | None,
) -> list[dict[str, Any]]:
    try:
        if cursor is None:
            rows = await get_pool().fetch(
                _records_sql(include_cursor=False),
                user_id,
                start,
                end,
                strength,
                actor_type,
                access_key_id,
                outcome,
                limit,
            )
        else:
            rows = await get_pool().fetch(
                _records_sql(include_cursor=True),
                user_id,
                start,
                end,
                strength,
                actor_type,
                access_key_id,
                outcome,
                limit,
                cursor[0],
                cursor[1],
            )
    except asyncpg.PostgresError as exc:
        logger.error("usage_records_query_failed", error_type=type(exc).__name__)
        raise DBError("Failed to query usage records") from exc
    return [dict(row) for row in rows]


__all__ = [
    "UsageEvent",
    "insert_usage_event",
    "query_latency",
    "query_usage_records",
    "query_usage_summary",
    "query_volume",
]
