"""Atomic PostgreSQL reservations for anonymous daily search quotas."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import cast

import asyncpg
import structlog

from scholight.db.client import DBError, get_pool
from scholight.models.quota import QuotaStatus, SearchStrengthValue

logger = structlog.get_logger(__name__)

_RESERVE_SQL = """
INSERT INTO scholight.anonymous_daily_search_usage AS usage (
    quota_date,
    ip_digest,
    strength,
    used_count,
    created_at,
    updated_at
)
SELECT
    (statement_timestamp() AT TIME ZONE 'UTC')::date,
    $1::bytea,
    $2::text,
    1,
    statement_timestamp(),
    statement_timestamp()
WHERE $3::integer > 0
ON CONFLICT (quota_date, ip_digest, strength)
DO UPDATE
SET
    used_count = usage.used_count + 1,
    updated_at = statement_timestamp()
WHERE usage.used_count < $3::integer
RETURNING quota_date, used_count
"""

_DECREMENT_SQL = """
UPDATE scholight.anonymous_daily_search_usage
SET
    used_count = used_count - 1,
    updated_at = statement_timestamp()
WHERE quota_date = $1
  AND ip_digest = $2
  AND strength = $3
  AND used_count > 0
RETURNING used_count
"""

_STATUS_SQL = """
SELECT
    $2::text AS strength,
    $3::integer AS daily_limit,
    COALESCE(usage.used_count, 0) AS used,
    GREATEST($3::integer - COALESCE(usage.used_count, 0), 0) AS remaining
FROM (SELECT 1) AS singleton
LEFT JOIN scholight.anonymous_daily_search_usage AS usage
    ON usage.ip_digest = $1::bytea
    AND usage.strength = $2::text
    AND usage.quota_date = (statement_timestamp() AT TIME ZONE 'UTC')::date
"""


@dataclass(frozen=True, slots=True)
class AnonymousQuotaReservation:
    """Identity required to compensate one successful anonymous reservation."""

    quota_date: date
    ip_digest: bytes
    strength: SearchStrengthValue
    used_count: int


def _validate_reservation_input(ip_digest: bytes, strength: str, limit: int) -> SearchStrengthValue:
    if len(ip_digest) != 32:
        raise ValueError("ip_digest must contain exactly 32 bytes")
    if strength not in {"standard", "thorough"}:
        raise ValueError("strength must be standard or thorough")
    if isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    return cast("SearchStrengthValue", strength)


async def reserve_anonymous_daily_quota(
    ip_digest: bytes,
    *,
    strength: str,
    limit: int,
) -> AnonymousQuotaReservation | None:
    """Atomically reserve one daily slot, or return ``None`` when exhausted."""
    normalized_strength = _validate_reservation_input(ip_digest, strength, limit)
    try:
        async with get_pool().acquire() as connection:
            row = await connection.fetchrow(
                _RESERVE_SQL,
                ip_digest,
                normalized_strength,
                limit,
            )
    except asyncpg.PostgresError as exc:
        logger.error("anonymous_quota_reserve_failed", error_type=type(exc).__name__)
        raise DBError("Failed to reserve anonymous search quota") from exc

    if row is None:
        return None
    return AnonymousQuotaReservation(
        quota_date=row["quota_date"],
        ip_digest=ip_digest,
        strength=normalized_strength,
        used_count=row["used_count"],
    )


async def decrement_anonymous_daily_quota(reservation: AnonymousQuotaReservation) -> bool:
    """Best-effort compensation primitive for one reservation; never retries."""
    try:
        async with get_pool().acquire() as connection:
            used_count = await connection.fetchval(
                _DECREMENT_SQL,
                reservation.quota_date,
                reservation.ip_digest,
                reservation.strength,
            )
    except asyncpg.PostgresError as exc:
        logger.warning("anonymous_quota_decrement_failed", error_type=type(exc).__name__)
        raise DBError("Failed to decrement anonymous search quota") from exc
    return used_count is not None


async def get_anonymous_quota_status(
    ip_digest: bytes,
    *,
    strength: str,
    limit: int,
) -> QuotaStatus:
    """Return the exact UTC-day quota status for one anonymous identity."""
    normalized_strength = _validate_reservation_input(ip_digest, strength, limit)
    try:
        async with get_pool().acquire() as connection:
            row = await connection.fetchrow(
                _STATUS_SQL,
                ip_digest,
                normalized_strength,
                limit,
            )
    except asyncpg.PostgresError as exc:
        logger.error("anonymous_quota_status_failed", error_type=type(exc).__name__)
        raise DBError("Failed to read anonymous search quota") from exc
    return QuotaStatus.model_validate(dict(row))


__all__ = [
    "AnonymousQuotaReservation",
    "decrement_anonymous_daily_quota",
    "get_anonymous_quota_status",
    "reserve_anonymous_daily_quota",
]
