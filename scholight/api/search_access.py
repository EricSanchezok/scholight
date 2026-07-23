"""Transport-neutral quota and rate-limit controls for public search."""

from __future__ import annotations

import hmac
import ipaddress
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

import structlog
from cloud_auth.models.user import UserRecord

from scholight.config import settings
from scholight.db.client import DBError
from scholight.db.queries_anonymous_quota import (
    AnonymousQuotaReservation,
    decrement_anonymous_daily_quota,
    reserve_anonymous_daily_quota,
)
from scholight.db.queries_quota import decrement_user_quota, reserve_user_quota
from scholight.models.quota import SearchStrengthValue, UserQuotaReservation

logger = structlog.get_logger(__name__)
_HMAC_CONTEXT = b"scholight:anonymous-quota:v1\0"
_MINUTE_SECONDS = 60
_minute_buckets: dict[bytes, tuple[int, int]] = {}


class SearchAccessError(Exception):
    """Stable quota/rate failure independent of an HTTP transport."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retry_after: int,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after


def anonymous_ip_digest(ip: str, secret: str) -> bytes:
    """Return a stable HMAC-SHA256 digest for a canonical client address."""
    address = ipaddress.ip_address(ip)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return hmac.digest(secret.encode("utf-8"), _HMAC_CONTEXT + address.packed, "sha256")


def reset_anonymous_minute_limits() -> None:
    """Clear process-local minute buckets for startup and isolated tests."""
    _minute_buckets.clear()


def _monotonic() -> float:
    return time.monotonic()


def check_anonymous_minute_limit(client_ip: str) -> None:
    """Consume one anonymous search attempt in the current fixed minute window."""
    now = _monotonic()
    window = int(now // _MINUTE_SECONDS)
    digest = anonymous_ip_digest(client_ip, settings.anonymous_quota_hmac_secret)
    stored_window, used = _minute_buckets.get(digest, (window, 0))
    if stored_window != window:
        used = 0
    if used >= settings.anonymous_rate_limit_per_minute:
        retry_after = max(1, math.ceil(((window + 1) * _MINUTE_SECONDS) - now))
        raise SearchAccessError(
            status_code=429,
            code="anonymous_rate_limit_exceeded",
            message="Anonymous search rate limit exceeded.",
            retry_after=retry_after,
        )
    _minute_buckets[digest] = (window, used + 1)
    if len(_minute_buckets) > 4096:
        stale = [
            key for key, (bucket_window, _) in _minute_buckets.items() if bucket_window != window
        ]
        for key in stale:
            del _minute_buckets[key]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _seconds_until_next_utc_day(now: datetime | None = None) -> int:
    current = now or _utc_now()
    tomorrow = datetime.combine(current.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    return max(1, math.ceil((tomorrow - current).total_seconds()))


def _access_error(
    *, status_code: int, code: str, message: str, retry_after: int
) -> SearchAccessError:
    return SearchAccessError(
        status_code=status_code,
        code=code,
        message=message,
        retry_after=retry_after,
    )


@dataclass(slots=True)
class SearchQuotaReservation:
    """A successful quota reservation and its one-shot compensation identity."""

    strength: SearchStrengthValue
    user: UserQuotaReservation | None = None
    anonymous: AnonymousQuotaReservation | None = None
    compensated: bool = False


async def reserve_search_quota(
    client_ip: str | None,
    current_user: UserRecord | None,
    *,
    strength: str,
) -> SearchQuotaReservation:
    """Reserve the correct user or anonymous quota before core search."""
    if strength not in {"standard", "thorough"}:
        raise ValueError("strength must be standard or thorough")
    normalized_strength = cast("SearchStrengthValue", strength)
    if current_user is not None:
        default_limit = (
            settings.authenticated_standard_daily_limit
            if normalized_strength == "standard"
            else settings.authenticated_thorough_daily_limit
        )
        try:
            user_reservation = await reserve_user_quota(
                current_user.id,
                strength=normalized_strength,
                default_limit=default_limit,
            )
        except DBError as exc:
            raise _access_error(
                status_code=503,
                code="quota_service_unavailable",
                message="Search quota service is temporarily unavailable.",
                retry_after=5,
            ) from exc
        if user_reservation is not None:
            return SearchQuotaReservation(
                strength=normalized_strength,
                user=user_reservation,
            )
        raise _access_error(
            status_code=429,
            code="user_daily_quota_exceeded",
            message="Daily search quota exceeded.",
            retry_after=_seconds_until_next_utc_day(),
        )

    if client_ip is None:
        raise _access_error(
            status_code=503,
            code="quota_service_unavailable",
            message="Search quota service is temporarily unavailable.",
            retry_after=5,
        )
    check_anonymous_minute_limit(client_ip)
    digest = anonymous_ip_digest(client_ip, settings.anonymous_quota_hmac_secret)
    limit = (
        settings.anonymous_standard_daily_limit
        if normalized_strength == "standard"
        else settings.anonymous_thorough_daily_limit
    )
    try:
        anonymous = await reserve_anonymous_daily_quota(
            digest,
            strength=normalized_strength,
            limit=limit,
        )
    except DBError as exc:
        raise _access_error(
            status_code=503,
            code="quota_service_unavailable",
            message="Search quota service is temporarily unavailable.",
            retry_after=5,
        ) from exc
    if anonymous is None:
        raise _access_error(
            status_code=429,
            code="anonymous_daily_limit_exceeded",
            message="Anonymous daily search limit exceeded.",
            retry_after=_seconds_until_next_utc_day(),
        )
    return SearchQuotaReservation(strength=normalized_strength, anonymous=anonymous)


async def compensate_search_quota(reservation: SearchQuotaReservation) -> None:
    """Attempt at most one pre-commit compensation without masking the root failure."""
    if reservation.compensated:
        return
    reservation.compensated = True
    if reservation.anonymous is not None:
        try:
            await decrement_anonymous_daily_quota(reservation.anonymous)
        except DBError:
            logger.warning("anonymous_search_quota_compensation_failed")
        return
    if reservation.user is None:
        return
    try:
        await decrement_user_quota(reservation.user)
    except DBError:
        logger.warning("user_search_quota_compensation_failed")


__all__ = [
    "SearchAccessError",
    "SearchQuotaReservation",
    "anonymous_ip_digest",
    "check_anonymous_minute_limit",
    "compensate_search_quota",
    "reserve_search_quota",
    "reset_anonymous_minute_limits",
]
