"""Authentication-adjacent access control for public search requests."""

from __future__ import annotations

import hmac
import ipaddress
import math
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import structlog
from cloud_auth.db.queries_quota import check_and_increment_quota, decrement_quota
from cloud_auth.exceptions import DBError as AuthDBError
from cloud_auth.models.user import UserRecord
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter

from scholight.config import settings
from scholight.db.client import DBError, get_pool
from scholight.db.queries_anonymous_quota import (
    AnonymousQuotaReservation,
    decrement_anonymous_daily_quota,
    reserve_anonymous_daily_quota,
)

logger = structlog.get_logger(__name__)
_HMAC_CONTEXT = b"scholight:anonymous-quota:v1\0"


def anonymous_ip_digest(ip: str, secret: str) -> bytes:
    """Return a stable HMAC-SHA256 digest for a canonical client address."""
    address = ipaddress.ip_address(ip)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return hmac.digest(secret.encode("utf-8"), _HMAC_CONTEXT + address.packed, "sha256")


def _anonymous_minute_key(request: Request) -> str:
    if (
        request.method != "POST"
        or request.url.path != "/search"
        or "authorization" in request.headers
    ):
        return ""
    if request.client is None:
        return "anonymous-client-unavailable"
    digest = anonymous_ip_digest(request.client.host, settings.anonymous_quota_hmac_secret)
    return digest[:16].hex()


def _minute_limit() -> str:
    return f"{settings.anonymous_rate_limit_per_minute}/minute"


anonymous_search_limiter = Limiter(
    key_func=_anonymous_minute_key,
    default_limits=[_minute_limit],
)


def anonymous_rate_limit_exceeded_handler(request: Request, _exc: Exception) -> JSONResponse:
    """Return the stable public anonymous-minute-limit error."""
    response = JSONResponse(
        status_code=429,
        content={
            "detail": {
                "code": "anonymous_rate_limit_exceeded",
                "message": "Anonymous search rate limit exceeded.",
                "retryable": True,
            }
        },
    )
    limit_item, arguments = request.state.view_rate_limit
    reset_at, _remaining = anonymous_search_limiter.limiter.get_window_stats(limit_item, *arguments)
    response.headers["Retry-After"] = str(max(1, math.ceil(reset_at - time.time())))
    return response


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _seconds_until_next_utc_day(now: datetime | None = None) -> int:
    current = now or _utc_now()
    tomorrow = datetime.combine(current.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    return max(1, math.ceil((tomorrow - current).total_seconds()))


def _retryable_error(
    *, status_code: int, code: str, message: str, retry_after: int
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "retryable": True},
        headers={"Retry-After": str(retry_after)},
    )


@dataclass(slots=True)
class SearchQuotaReservation:
    """A successful quota reservation and its one-shot compensation identity."""

    operation: str
    user_id: int | None = None
    user_quota_date: date | None = None
    user_quota_completed_date: date | None = None
    anonymous: AnonymousQuotaReservation | None = None
    compensated: bool = False


async def reserve_search_quota(
    request: Request,
    current_user: UserRecord | None,
    *,
    search_level: int,
) -> SearchQuotaReservation:
    """Reserve the correct user or anonymous daily quota before core search."""
    operation = f"search_level{search_level}"
    if current_user is not None:
        quota_date = _utc_now().date()
        try:
            result = await check_and_increment_quota(get_pool, current_user.id, operation)
        except AuthDBError as exc:
            raise _retryable_error(
                status_code=503,
                code="quota_service_unavailable",
                message="Search quota service is temporarily unavailable.",
                retry_after=5,
            ) from exc
        quota_completed_date = _utc_now().date()
        reservation = SearchQuotaReservation(
            operation=operation,
            user_id=current_user.id,
            user_quota_date=quota_date,
            user_quota_completed_date=quota_completed_date,
        )
        if result.allowed:
            return reservation
        await compensate_search_quota(reservation)
        raise _retryable_error(
            status_code=429,
            code="user_daily_quota_exceeded",
            message="Daily search quota exceeded.",
            retry_after=_seconds_until_next_utc_day(),
        )

    if request.client is None:
        raise _retryable_error(
            status_code=503,
            code="quota_service_unavailable",
            message="Search quota service is temporarily unavailable.",
            retry_after=5,
        )
    digest = anonymous_ip_digest(request.client.host, settings.anonymous_quota_hmac_secret)
    limit = (
        settings.anonymous_standard_daily_limit
        if search_level == 1
        else settings.anonymous_thorough_daily_limit
    )
    try:
        anonymous = await reserve_anonymous_daily_quota(
            digest,
            search_level=search_level,
            limit=limit,
        )
    except DBError as exc:
        raise _retryable_error(
            status_code=503,
            code="quota_service_unavailable",
            message="Search quota service is temporarily unavailable.",
            retry_after=5,
        ) from exc
    if anonymous is None:
        raise _retryable_error(
            status_code=429,
            code="anonymous_daily_limit_exceeded",
            message="Anonymous daily search limit exceeded.",
            retry_after=_seconds_until_next_utc_day(),
        )
    return SearchQuotaReservation(operation=operation, anonymous=anonymous)


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
    if reservation.user_id is None:
        return
    if reservation.user_quota_date != reservation.user_quota_completed_date:
        logger.warning(
            "user_search_quota_compensation_skipped",
            reason="quota_check_crossed_utc_date",
        )
        return
    if reservation.user_quota_completed_date != _utc_now().date():
        logger.warning("user_search_quota_compensation_skipped", reason="utc_date_changed")
        return
    await decrement_quota(get_pool, reservation.user_id, reservation.operation)


__all__ = [
    "SearchQuotaReservation",
    "anonymous_ip_digest",
    "anonymous_rate_limit_exceeded_handler",
    "anonymous_search_limiter",
    "compensate_search_quota",
    "reserve_search_quota",
]
