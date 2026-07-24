"""Administrator-only aggregate Scholight product analytics API."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Annotated

from cloud_auth.models.user import UserRecord
from fastapi import APIRouter, Depends, HTTPException, Query

from scholight.api.deps import get_scholight_admin
from scholight.api.models.admin_metrics import AdminAnalyticsResponse
from scholight.db.client import DBError
from scholight.db.queries_admin_analytics import query_admin_analytics

router = APIRouter()


@router.get("/overview", response_model=AdminAnalyticsResponse)
async def analytics_overview(
    days: Annotated[int, Query(ge=1, le=90)] = 30,
    _admin: UserRecord = Depends(get_scholight_admin),
) -> AdminAnalyticsResponse:
    end = datetime.combine(datetime.now(UTC).date() + timedelta(days=1), time.min, tzinfo=UTC)
    start = end - timedelta(days=days)
    try:
        metrics = await query_admin_analytics(start=start, end=end)
    except DBError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "admin_analytics_unavailable",
                "message": "Product analytics are temporarily unavailable.",
                "retryable": True,
            },
            headers={"Retry-After": "5"},
        ) from exc
    return AdminAnalyticsResponse.model_validate({"from": start, "to": end, **metrics})


__all__ = ["router"]
