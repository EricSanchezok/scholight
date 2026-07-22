"""JWT-only usage analytics and CSV export routes."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from cloud_auth.db.asyncpg import AsyncpgUserDatabase
from cloud_auth.models.user import QuotaStatus, UserRecord
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from scholight.api.deps import get_current_user
from scholight.api.models.usage import (
    DailyQuotaUsage,
    LatencyResponse,
    TodayUsage,
    UsageAccessKey,
    UsageRecord,
    UsageRecordsResponse,
    UsageSummaryResponse,
    VolumeResponse,
)
from scholight.api.usage import (
    UsageRangeError,
    decode_usage_cursor,
    encode_usage_cursor,
    escape_csv_cell,
    fill_latency_days,
    fill_volume_days,
    parse_range_value,
    resolve_usage_range,
)
from scholight.db.client import DBError, get_pool
from scholight.db.queries_usage import (
    query_latency,
    query_usage_records,
    query_usage_summary,
    query_volume,
)

router = APIRouter()
_EXPORT_LIMIT = 10_000


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "retryable": False},
    )


def _range(from_: str | None, to: str | None) -> tuple[datetime, datetime]:
    try:
        return resolve_usage_range(parse_range_value(from_), parse_range_value(to))
    except UsageRangeError as exc:
        message = (
            "Usage range exceeds 13 months."
            if exc.code == "usage_range_too_large"
            else "Invalid usage range."
        )
        raise _error(400, exc.code, message) from exc


def _quota(value: QuotaStatus | None) -> DailyQuotaUsage:
    if value is None:
        return DailyQuotaUsage(used=0, daily_limit=0, remaining=0)
    return DailyQuotaUsage(
        used=value.used,
        daily_limit=value.daily_limit,
        remaining=value.remaining,
    )


@router.get("/summary", response_model=UsageSummaryResponse)
async def usage_summary(
    current_user: UserRecord = Depends(get_current_user),
) -> UsageSummaryResponse:
    db = AsyncpgUserDatabase(pool_factory=get_pool)
    try:
        quotas = await db.get_quota_status(current_user.id)
        stats = await query_usage_summary(current_user.id)
    except Exception as exc:
        raise _error(503, "usage_service_unavailable", "Usage service unavailable.") from exc
    by_operation = {quota.operation: quota for quota in quotas}
    standard = _quota(by_operation.get("search_level1"))
    thorough = _quota(by_operation.get("search_level2"))
    success = int(stats["success_count"])
    degraded = int(stats["degraded_count"])
    failed = int(stats["failed_count"])
    attempts = success + degraded + failed
    tomorrow = datetime.now(UTC).date() + timedelta(days=1)
    return UsageSummaryResponse(
        reset_at=datetime.combine(tomorrow, datetime.min.time(), tzinfo=UTC),
        today=TodayUsage(standard=standard, thorough=thorough),
        searches_today=standard.used + thorough.used,
        searches_this_month=int(stats["searches_this_month"]),
        typical_response_ms=(
            float(stats["typical_response_ms"])
            if stats["typical_response_ms"] is not None
            else None
        ),
        p95_response_ms=(
            float(stats["p95_response_ms"]) if stats["p95_response_ms"] is not None else None
        ),
        success_rate=success / attempts if attempts else None,
        degraded_count=degraded,
        failed_count=failed,
    )


@router.get("/volume", response_model=VolumeResponse)
async def usage_volume(
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    bucket: Literal["day"] = "day",
    access_key_id: UUID | None = None,
    current_user: UserRecord = Depends(get_current_user),
) -> VolumeResponse:
    start, end = _range(from_, to)
    try:
        rows = await query_volume(
            current_user.id, start=start, end=end, access_key_id=access_key_id
        )
    except DBError as exc:
        raise _error(503, "usage_service_unavailable", "Usage service unavailable.") from exc
    return VolumeResponse.model_validate(
        {"from": start, "to": end, "bucket": bucket, "points": fill_volume_days(start, end, rows)}
    )


@router.get("/latency", response_model=LatencyResponse)
async def usage_latency(
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    bucket: Literal["day"] = "day",
    access_key_id: UUID | None = None,
    current_user: UserRecord = Depends(get_current_user),
) -> LatencyResponse:
    start, end = _range(from_, to)
    try:
        rows = await query_latency(
            current_user.id, start=start, end=end, access_key_id=access_key_id
        )
    except DBError as exc:
        raise _error(503, "usage_service_unavailable", "Usage service unavailable.") from exc
    return LatencyResponse.model_validate(
        {
            "from": start,
            "to": end,
            "bucket": bucket,
            "points": fill_latency_days(start, end, rows),
        }
    )


def _map_record(row: dict[str, Any]) -> UsageRecord:
    key = None
    if row["access_key_id"] is not None:
        key = UsageAccessKey(
            id=UUID(str(row["access_key_id"])),
            name=str(row["access_key_name"]),
            last4=str(row["access_key_last4"]),
        )
    return UsageRecord(
        id=int(row["id"]),
        created_at=row["created_at"],
        actor_type=row["actor_type"],
        access_key=key,
        strength=row["strength"],
        search_duration_ms=row["search_duration_ms"],
        result_count=row["result_count"],
        outcome=row["outcome"],
        quota_units=int(row["quota_units"]),
        status_code=row["status_code"],
        error_code=str(row["error_code"]) if row["error_code"] is not None else None,
    )


async def _records(
    *,
    user_id: int,
    from_: str | None,
    to: str | None,
    limit: int,
    strength: str | None,
    actor_type: str | None,
    access_key_id: UUID | None,
    outcome: str | None,
    cursor: str | None,
) -> tuple[list[dict[str, Any]], datetime, datetime]:
    start, end = _range(from_, to)
    try:
        decoded = decode_usage_cursor(cursor) if cursor else None
        rows = await query_usage_records(
            user_id,
            start=start,
            end=end,
            limit=limit,
            strength=strength,
            actor_type=actor_type,
            access_key_id=access_key_id,
            outcome=outcome,
            cursor=decoded,
        )
    except UsageRangeError as exc:
        raise _error(400, exc.code, "Invalid usage cursor.") from exc
    except DBError as exc:
        raise _error(503, "usage_service_unavailable", "Usage service unavailable.") from exc
    return rows, start, end


@router.get("/records", response_model=UsageRecordsResponse)
async def usage_records(
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    strength: Literal["standard", "thorough"] | None = None,
    actor_type: Literal["web", "access_key"] | None = None,
    access_key_id: UUID | None = None,
    outcome: Literal["success", "degraded", "failed"] | None = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    current_user: UserRecord = Depends(get_current_user),
) -> UsageRecordsResponse:
    rows, _start, _end = await _records(
        user_id=current_user.id,
        from_=from_,
        to=to,
        limit=limit + 1,
        strength=strength,
        actor_type=actor_type,
        access_key_id=access_key_id,
        outcome=outcome,
        cursor=cursor,
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if has_more and page:
        next_cursor = encode_usage_cursor(page[-1]["created_at"], int(page[-1]["id"]))
    return UsageRecordsResponse(items=[_map_record(row) for row in page], next_cursor=next_cursor)


@router.get("/export.csv")
async def usage_export(
    strength: Literal["standard", "thorough"] | None = None,
    actor_type: Literal["web", "access_key"] | None = None,
    access_key_id: UUID | None = None,
    outcome: Literal["success", "degraded", "failed"] | None = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    current_user: UserRecord = Depends(get_current_user),
) -> Response:
    rows, _start, _end = await _records(
        user_id=current_user.id,
        from_=from_,
        to=to,
        limit=_EXPORT_LIMIT + 1,
        strength=strength,
        actor_type=actor_type,
        access_key_id=access_key_id,
        outcome=outcome,
        cursor=None,
    )
    if len(rows) > _EXPORT_LIMIT:
        raise _error(413, "usage_export_too_large", "Usage export exceeds 10000 rows.")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "id",
            "created_at",
            "actor_type",
            "access_key_name",
            "access_key_last4",
            "strength",
            "search_duration_ms",
            "result_count",
            "outcome",
            "quota_units",
            "status_code",
            "error_code",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row["id"],
                row["created_at"],
                row["actor_type"],
                escape_csv_cell(row["access_key_name"] or ""),
                row["access_key_last4"] or "",
                row["strength"],
                row["search_duration_ms"],
                row["result_count"],
                row["outcome"],
                row["quota_units"],
                row["status_code"],
                escape_csv_cell(row["error_code"] or ""),
            ]
        )
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="scholight-usage.csv"'},
    )


__all__ = ["router"]
