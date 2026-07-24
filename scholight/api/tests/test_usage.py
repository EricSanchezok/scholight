"""Usage analytics range, cursor, bucket, and export tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from cloud_auth.models.user import UserRecord
from fastapi import FastAPI

from scholight.api.deps import SearchActor
from scholight.api.routes.usage import usage_summary
from scholight.api.search_execution import _schedule_usage
from scholight.api.usage import (
    UsageRangeError,
    decode_usage_cursor,
    encode_usage_cursor,
    escape_csv_cell,
    fill_latency_days,
    fill_volume_days,
    resolve_usage_range,
)
from scholight.models.quota import QuotaStatus


def test_usage_csv_openapi_declares_text_response(api_app: FastAPI) -> None:
    content = api_app.openapi()["paths"]["/user/usage/export.csv"]["get"]["responses"]["200"][
        "content"
    ]

    assert "text/csv" in content


def test_default_usage_range_contains_thirty_utc_days() -> None:
    start, end = resolve_usage_range(None, None, now=datetime(2026, 7, 22, 18, tzinfo=UTC))

    assert (start, end) == (
        datetime(2026, 6, 23, tzinfo=UTC),
        datetime(2026, 7, 23, tzinfo=UTC),
    )


def test_usage_range_rejects_more_than_thirteen_months() -> None:
    with pytest.raises(UsageRangeError, match="usage_range_too_large"):
        resolve_usage_range(date(2025, 1, 1), date(2026, 7, 1))


def test_volume_days_fill_missing_dates_with_zero() -> None:
    points = fill_volume_days(
        datetime(2026, 7, 20, tzinfo=UTC),
        datetime(2026, 7, 23, tzinfo=UTC),
        [{"bucket_start": datetime(2026, 7, 21, tzinfo=UTC), "standard": 2, "thorough": 1}],
    )

    assert [(point.standard, point.thorough) for point in points] == [(0, 0), (2, 1), (0, 0)]


def test_latency_days_use_null_not_zero_for_empty_buckets() -> None:
    points = fill_latency_days(
        datetime(2026, 7, 20, tzinfo=UTC),
        datetime(2026, 7, 22, tzinfo=UTC),
        [],
    )

    assert points[0].standard_p50_ms is None
    assert points[0].thorough_p50_ms is None
    assert points[0].overall_p95_ms is None
    assert points[0].sample_count == 0


def test_usage_cursor_round_trip() -> None:
    created_at = datetime(2026, 7, 22, 18, 42, tzinfo=UTC)

    cursor = encode_usage_cursor(created_at, 123)

    assert decode_usage_cursor(cursor) == (created_at, 123)


def test_usage_cursor_rejects_malformed_value() -> None:
    with pytest.raises(UsageRangeError, match="invalid_usage_cursor"):
        decode_usage_cursor("not-a-cursor")


@pytest.mark.parametrize("value", ["=1+1", "+cmd", "-2+3", "@SUM(A1)"])
def test_csv_formula_injection_is_escaped(value: str) -> None:
    assert escape_csv_cell(value).startswith("'")


def test_csv_normal_text_is_unchanged() -> None:
    assert escape_csv_cell("literature-review") == "literature-review"


@pytest.mark.parametrize(
    ("outcome", "quota_units", "result_count"),
    [("success", 1, 10), ("degraded", 1, 8), ("failed", 0, None)],
)
def test_authenticated_usage_event_fields(
    active_user: UserRecord,
    outcome: str,
    quota_units: int,
    result_count: int | None,
) -> None:
    key_id = uuid4()
    actor = SearchActor(user=active_user, actor_type="access_key", access_key_id=key_id)

    with patch("scholight.api.search_execution.schedule_usage_event") as schedule:
        _schedule_usage(
            actor,
            transport="mcp",
            request_id="request-1",
            strength="thorough",
            outcome=outcome,  # type: ignore[arg-type]
            quota_units=quota_units,
            result_count=result_count,
            duration_ms=900.0,
            status_code=200 if outcome != "failed" else 500,
            error_code=None if outcome != "failed" else "search_failed",
        )

    event = schedule.call_args.args[0]
    assert (event.actor_type, event.access_key_id, event.transport) == (
        "access_key",
        key_id,
        "mcp",
    )
    assert (event.outcome, event.quota_units, event.result_count) == (
        outcome,
        quota_units,
        result_count,
    )
    assert not hasattr(event, "query_text")


def test_anonymous_or_rejected_request_schedules_no_usage_event() -> None:
    with patch("scholight.api.search_execution.schedule_usage_event") as schedule:
        _schedule_usage(
            None,
            transport="rest",
            request_id="request-1",
            strength="standard",
            outcome="failed",
            quota_units=0,
            result_count=None,
            duration_ms=None,
            status_code=429,
            error_code="anonymous_daily_limit_exceeded",
        )

    schedule.assert_not_called()


@pytest.mark.asyncio
async def test_summary_uses_daily_quota_and_monthly_event_statistics(
    active_user: UserRecord,
) -> None:
    quotas = [
        QuotaStatus(strength="standard", daily_limit=100, used=18, remaining=82),
        QuotaStatus(strength="thorough", daily_limit=30, used=4, remaining=26),
    ]
    stats = {
        "searches_this_month": 184,
        "typical_response_ms": 840.0,
        "p95_response_ms": 3120.0,
        "success_count": 125,
        "degraded_count": 2,
        "failed_count": 1,
    }

    with (
        patch(
            "scholight.api.routes.usage.get_user_quota_status",
            AsyncMock(return_value=quotas),
        ),
        patch("scholight.api.routes.usage.query_usage_summary", AsyncMock(return_value=stats)),
    ):
        summary = await usage_summary(active_user)

    assert summary.searches_today == 22
    assert summary.searches_this_month == 184
    assert summary.typical_response_ms == 840.0
    assert summary.success_rate == 125 / 128
