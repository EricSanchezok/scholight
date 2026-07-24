"""Aggregate, content-free Scholight product analytics."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg
import structlog

from scholight.db.client import DBError, get_pool

logger = structlog.get_logger(__name__)


def _integer(row: dict[str, Any], key: str) -> int:
    return int(row.get(key) or 0)


async def query_admin_analytics(*, start: datetime, end: datetime) -> dict[str, Any]:
    """Aggregate only ``scholight.*`` state; never return identity or search content."""
    pool = get_pool()
    try:
        profile_record = await pool.fetchrow(
            """
            SELECT
                count(*)::BIGINT AS total,
                count(*) FILTER (WHERE status = 'active')::BIGINT AS active,
                count(*) FILTER (WHERE status = 'blocked')::BIGINT AS blocked,
                count(*) FILTER (WHERE is_admin IS TRUE)::BIGINT AS admins,
                count(*) FILTER (
                    WHERE created_at >= $1 AND created_at < $2
                )::BIGINT AS created_in_period
            FROM scholight.user_profiles
            """,
            start,
            end,
        )
        usage_record = await pool.fetchrow(
            """
            SELECT
                COALESCE(sum(quota_units), 0)::BIGINT AS authenticated,
                COALESCE(sum(quota_units) FILTER (
                    WHERE strength = 'standard'
                ), 0)::BIGINT AS standard,
                COALESCE(sum(quota_units) FILTER (
                    WHERE strength = 'thorough'
                ), 0)::BIGINT AS thorough,
                COALESCE(sum(quota_units) FILTER (
                    WHERE transport = 'rest'
                ), 0)::BIGINT AS rest,
                COALESCE(sum(quota_units) FILTER (
                    WHERE transport = 'mcp'
                ), 0)::BIGINT AS mcp,
                count(*) FILTER (WHERE outcome = 'success')::BIGINT AS success,
                count(*) FILTER (WHERE outcome = 'degraded')::BIGINT AS degraded,
                count(*) FILTER (WHERE outcome = 'failed')::BIGINT AS failed,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY search_duration_ms)
                    FILTER (
                        WHERE outcome IN ('success', 'degraded')
                          AND search_duration_ms IS NOT NULL
                    ) AS p50_response_ms,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY search_duration_ms)
                    FILTER (
                        WHERE outcome IN ('success', 'degraded')
                          AND search_duration_ms IS NOT NULL
                    ) AS p95_response_ms
            FROM scholight.usage_events
            WHERE created_at >= $1 AND created_at < $2
            """,
            start,
            end,
        )
        anonymous_record = await pool.fetchrow(
            """
            SELECT
                COALESCE(sum(used_count), 0)::BIGINT AS anonymous,
                COALESCE(sum(used_count) FILTER (
                    WHERE strength = 'standard'
                ), 0)::BIGINT AS standard,
                COALESCE(sum(used_count) FILTER (
                    WHERE strength = 'thorough'
                ), 0)::BIGINT AS thorough
            FROM scholight.anonymous_daily_search_usage
            WHERE quota_date >= ($1 AT TIME ZONE 'UTC')::date
              AND quota_date < ($2 AT TIME ZONE 'UTC')::date
            """,
            start,
            end,
        )
        key_record = await pool.fetchrow(
            """
            SELECT
                count(*)::BIGINT AS total,
                count(*) FILTER (
                    WHERE revoked_at IS NULL
                      AND (expires_at IS NULL OR expires_at > statement_timestamp())
                )::BIGINT AS active,
                count(*) FILTER (
                    WHERE last_used_at >= $1 AND last_used_at < $2
                )::BIGINT AS used_in_period
            FROM scholight.access_keys
            """,
            start,
            end,
        )
        daily_rows = await pool.fetch(
            """
            WITH days AS (
                SELECT generate_series(
                    ($1 AT TIME ZONE 'UTC')::date,
                    (($2 AT TIME ZONE 'UTC')::date - 1),
                    interval '1 day'
                )::date AS day
            ),
            authenticated AS (
                SELECT (created_at AT TIME ZONE 'UTC')::date AS day,
                       COALESCE(sum(quota_units), 0)::BIGINT AS total,
                       COALESCE(sum(quota_units) FILTER (
                           WHERE strength = 'standard'
                       ), 0)::BIGINT AS standard,
                       COALESCE(sum(quota_units) FILTER (
                           WHERE strength = 'thorough'
                       ), 0)::BIGINT AS thorough,
                       COALESCE(sum(quota_units) FILTER (
                           WHERE transport = 'rest'
                       ), 0)::BIGINT AS rest,
                       COALESCE(sum(quota_units) FILTER (
                           WHERE transport = 'mcp'
                       ), 0)::BIGINT AS mcp
                FROM scholight.usage_events
                WHERE created_at >= $1 AND created_at < $2
                GROUP BY day
            ),
            anonymous AS (
                SELECT quota_date AS day,
                       COALESCE(sum(used_count), 0)::BIGINT AS total,
                       COALESCE(sum(used_count) FILTER (
                           WHERE strength = 'standard'
                       ), 0)::BIGINT AS standard,
                       COALESCE(sum(used_count) FILTER (
                           WHERE strength = 'thorough'
                       ), 0)::BIGINT AS thorough
                FROM scholight.anonymous_daily_search_usage
                WHERE quota_date >= ($1 AT TIME ZONE 'UTC')::date
                  AND quota_date < ($2 AT TIME ZONE 'UTC')::date
                GROUP BY quota_date
            )
            SELECT days.day,
                   COALESCE(authenticated.total, 0)::BIGINT AS authenticated,
                   COALESCE(anonymous.total, 0)::BIGINT AS anonymous,
                   (
                       COALESCE(authenticated.total, 0)
                       + COALESCE(anonymous.total, 0)
                   )::BIGINT AS total,
                   (
                       COALESCE(authenticated.standard, 0)
                       + COALESCE(anonymous.standard, 0)
                   )::BIGINT AS standard,
                   (
                       COALESCE(authenticated.thorough, 0)
                       + COALESCE(anonymous.thorough, 0)
                   )::BIGINT AS thorough,
                   COALESCE(authenticated.rest, 0)::BIGINT AS authenticated_rest,
                   COALESCE(authenticated.mcp, 0)::BIGINT AS authenticated_mcp
            FROM days
            LEFT JOIN authenticated USING (day)
            LEFT JOIN anonymous USING (day)
            ORDER BY days.day
            """,
            start,
            end,
        )
    except asyncpg.PostgresError as exc:
        logger.error("admin_analytics_query_failed", error_type=type(exc).__name__)
        raise DBError("Failed to query Scholight analytics") from exc

    profiles = dict(profile_record) if profile_record is not None else {}
    usage = dict(usage_record) if usage_record is not None else {}
    anonymous = dict(anonymous_record) if anonymous_record is not None else {}
    access_keys = dict(key_record) if key_record is not None else {}
    authenticated = _integer(usage, "authenticated")
    anonymous_total = _integer(anonymous, "anonymous")
    return {
        "profiles": {
            "total": _integer(profiles, "total"),
            "active": _integer(profiles, "active"),
            "blocked": _integer(profiles, "blocked"),
            "admins": _integer(profiles, "admins"),
            "created_in_period": _integer(profiles, "created_in_period"),
        },
        "searches": {
            "total": authenticated + anonymous_total,
            "authenticated": authenticated,
            "anonymous": anonymous_total,
            "standard": _integer(usage, "standard") + _integer(anonymous, "standard"),
            "thorough": _integer(usage, "thorough") + _integer(anonymous, "thorough"),
            "authenticated_rest": _integer(usage, "rest"),
            "authenticated_mcp": _integer(usage, "mcp"),
            "authenticated_success": _integer(usage, "success"),
            "authenticated_degraded": _integer(usage, "degraded"),
            "authenticated_failed": _integer(usage, "failed"),
            "authenticated_p50_response_ms": usage.get("p50_response_ms"),
            "authenticated_p95_response_ms": usage.get("p95_response_ms"),
        },
        "access_keys": {
            "total": _integer(access_keys, "total"),
            "active": _integer(access_keys, "active"),
            "used_in_period": _integer(access_keys, "used_in_period"),
        },
        "daily": [dict(row) for row in daily_rows],
    }


__all__ = ["query_admin_analytics"]
