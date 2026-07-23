"""Atomic Scholight-owned authenticated search quota operations."""

from __future__ import annotations

from typing import cast

import asyncpg
import structlog

from scholight.db.client import DBError, get_pool
from scholight.models.quota import QuotaStatus, SearchStrengthValue, UserQuotaReservation

logger = structlog.get_logger(__name__)

_RESERVE_SQL = """
WITH effective_quota AS (
    SELECT COALESCE(
        (
            SELECT daily_limit
            FROM scholight.user_quota_overrides
            WHERE user_id = $1 AND strength = $2
        ),
        $3::integer
    ) AS daily_limit
),
reservation AS (
    INSERT INTO scholight.user_daily_search_usage AS usage (
        quota_date,
        user_id,
        strength,
        used_count,
        created_at,
        updated_at
    )
    SELECT
        (statement_timestamp() AT TIME ZONE 'UTC')::date,
        $1,
        $2,
        1,
        statement_timestamp(),
        statement_timestamp()
    FROM effective_quota
    WHERE daily_limit > 0
    ON CONFLICT (quota_date, user_id, strength)
    DO UPDATE
    SET
        used_count = usage.used_count + 1,
        updated_at = statement_timestamp()
    WHERE usage.used_count < (SELECT daily_limit FROM effective_quota)
    RETURNING quota_date, used_count
)
SELECT effective_quota.daily_limit, reservation.quota_date, reservation.used_count
FROM effective_quota
LEFT JOIN reservation ON TRUE
"""

_STATUS_SQL = """
WITH configured(strength, default_limit) AS (
    VALUES
        ('standard'::text, $2::integer),
        ('thorough'::text, $3::integer)
)
SELECT
    configured.strength,
    COALESCE(overrides.daily_limit, configured.default_limit) AS daily_limit,
    COALESCE(usage.used_count, 0) AS used,
    GREATEST(
        COALESCE(overrides.daily_limit, configured.default_limit)
            - COALESCE(usage.used_count, 0),
        0
    ) AS remaining
FROM configured
LEFT JOIN scholight.user_quota_overrides AS overrides
    ON overrides.user_id = $1 AND overrides.strength = configured.strength
LEFT JOIN scholight.user_daily_search_usage AS usage
    ON usage.user_id = $1
    AND usage.strength = configured.strength
    AND usage.quota_date = (statement_timestamp() AT TIME ZONE 'UTC')::date
ORDER BY configured.strength
"""

_DECREMENT_SQL = """
UPDATE scholight.user_daily_search_usage
SET
    used_count = used_count - 1,
    updated_at = statement_timestamp()
WHERE quota_date = $1
  AND user_id = $2
  AND strength = $3
  AND used_count > 0
RETURNING used_count
"""


def _validate_strength(strength: str) -> SearchStrengthValue:
    if strength not in {"standard", "thorough"}:
        raise ValueError("strength must be standard or thorough")
    return cast("SearchStrengthValue", strength)


async def reserve_user_quota(
    user_id: int,
    *,
    strength: str,
    default_limit: int,
) -> UserQuotaReservation | None:
    """Reserve one authenticated search without incrementing beyond the limit."""
    normalized_strength = _validate_strength(strength)
    if isinstance(default_limit, bool) or default_limit < 0:
        raise ValueError("default_limit must be a non-negative integer")
    try:
        row = await get_pool().fetchrow(
            _RESERVE_SQL,
            user_id,
            normalized_strength,
            default_limit,
        )
    except asyncpg.PostgresError as exc:
        logger.error("user_quota_reserve_failed", error_type=type(exc).__name__)
        raise DBError("Failed to reserve user search quota") from exc
    if row is None or row["quota_date"] is None:
        return None
    return UserQuotaReservation(
        user_id=user_id,
        strength=normalized_strength,
        quota_date=row["quota_date"],
        used_count=int(row["used_count"]),
        daily_limit=int(row["daily_limit"]),
    )


async def decrement_user_quota(reservation: UserQuotaReservation) -> bool:
    """Compensate exactly the UTC-day reservation that succeeded."""
    try:
        used_count = await get_pool().fetchval(
            _DECREMENT_SQL,
            reservation.quota_date,
            reservation.user_id,
            reservation.strength,
        )
    except asyncpg.PostgresError as exc:
        logger.warning("user_quota_decrement_failed", error_type=type(exc).__name__)
        raise DBError("Failed to decrement user search quota") from exc
    return used_count is not None


async def get_user_quota_status(
    user_id: int,
    *,
    standard_default_limit: int,
    thorough_default_limit: int,
) -> list[QuotaStatus]:
    """Return effective defaults/overrides joined with today's UTC usage."""
    try:
        rows = await get_pool().fetch(
            _STATUS_SQL,
            user_id,
            standard_default_limit,
            thorough_default_limit,
        )
    except asyncpg.PostgresError as exc:
        logger.error("user_quota_status_failed", error_type=type(exc).__name__)
        raise DBError("Failed to read user search quota") from exc
    return [QuotaStatus.model_validate(dict(row)) for row in rows]


__all__ = [
    "decrement_user_quota",
    "get_user_quota_status",
    "reserve_user_quota",
]
