"""Pure helpers for bounded UTC usage analytics and safe exports."""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from scholight.api.models.usage import LatencyPoint, VolumePoint

_MAX_RANGE = timedelta(days=397)


class UsageRangeError(ValueError):
    """Stable validation error for range and cursor inputs."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _as_start(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.combine(value, time.min, tzinfo=UTC)


def _as_end(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.combine(value + timedelta(days=1), time.min, tzinfo=UTC)


def resolve_usage_range(
    from_value: date | datetime | None,
    to_value: date | datetime | None,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Resolve an inclusive date/exclusive datetime range capped at 13 months."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    default_end = datetime.combine(current.date() + timedelta(days=1), time.min, tzinfo=UTC)
    end = _as_end(to_value) if to_value is not None else default_end
    start = _as_start(from_value) if from_value is not None else end - timedelta(days=30)
    if start >= end:
        raise UsageRangeError("invalid_usage_range")
    if end - start > _MAX_RANGE:
        raise UsageRangeError("usage_range_too_large")
    return start, end


def fill_volume_days(
    start: datetime,
    end: datetime,
    rows: list[dict[str, Any]],
) -> list[VolumePoint]:
    indexed = {row["bucket_start"]: row for row in rows}
    points: list[VolumePoint] = []
    bucket = datetime.combine(start.date(), time.min, tzinfo=UTC)
    while bucket < end:
        row = indexed.get(bucket)
        points.append(
            VolumePoint(
                bucket_start=bucket,
                standard=int(row["standard"]) if row else 0,
                thorough=int(row["thorough"]) if row else 0,
            )
        )
        bucket += timedelta(days=1)
    return points


def _optional_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def fill_latency_days(
    start: datetime,
    end: datetime,
    rows: list[dict[str, Any]],
) -> list[LatencyPoint]:
    indexed = {row["bucket_start"]: row for row in rows}
    points: list[LatencyPoint] = []
    bucket = datetime.combine(start.date(), time.min, tzinfo=UTC)
    while bucket < end:
        row = indexed.get(bucket)
        points.append(
            LatencyPoint(
                bucket_start=bucket,
                standard_p50_ms=_optional_float(row.get("standard_p50_ms")) if row else None,
                thorough_p50_ms=_optional_float(row.get("thorough_p50_ms")) if row else None,
                overall_p95_ms=_optional_float(row.get("overall_p95_ms")) if row else None,
                sample_count=int(row["sample_count"]) if row else 0,
            )
        )
        bucket += timedelta(days=1)
    return points


def encode_usage_cursor(created_at: datetime, event_id: int) -> str:
    payload = f"{created_at.astimezone(UTC).isoformat()}|{event_id}".encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_usage_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8")
        timestamp, raw_id = raw.rsplit("|", 1)
        created_at = datetime.fromisoformat(timestamp)
        if created_at.tzinfo is None:
            raise ValueError
        event_id = int(raw_id)
        if event_id < 1:
            raise ValueError
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise UsageRangeError("invalid_usage_cursor") from exc
    return created_at.astimezone(UTC), event_id


def parse_range_value(value: str | None) -> date | datetime | None:
    if value is None:
        return None
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UsageRangeError("invalid_usage_range") from exc


def escape_csv_cell(value: Any) -> Any:
    """Prevent spreadsheet formula execution while preserving non-text values."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


__all__ = [
    "UsageRangeError",
    "decode_usage_cursor",
    "encode_usage_cursor",
    "escape_csv_cell",
    "fill_latency_days",
    "fill_volume_days",
    "parse_range_value",
    "resolve_usage_range",
]
