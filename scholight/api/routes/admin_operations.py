"""Administrator-only Scholight ingestion operations API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from cloud_auth.models.user import UserRecord
from fastapi import APIRouter, Depends, HTTPException, Query

from scholight.api.deps import get_scholight_admin
from scholight.api.models.admin_metrics import AdminOperationsResponse
from scholight.db.client import DBError
from scholight.db.queries_admin_operations import query_admin_operations

router = APIRouter()


@router.get("/overview", response_model=AdminOperationsResponse)
async def operations_overview(
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    issue_limit: Annotated[int, Query(ge=1, le=100)] = 20,
    _admin: UserRecord = Depends(get_scholight_admin),
) -> AdminOperationsResponse:
    try:
        metrics = await query_admin_operations(days=days, issue_limit=issue_limit)
    except DBError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "admin_operations_unavailable",
                "message": "Operations metrics are temporarily unavailable.",
                "retryable": True,
            },
            headers={"Retry-After": "5"},
        ) from exc
    return AdminOperationsResponse(
        generated_at=datetime.now(UTC),
        **metrics,
    )


__all__ = ["router"]
