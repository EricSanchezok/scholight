"""Search routes — semantic search, search history."""

from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sanchezcloud_identity.models.auth import MessageResponse
from sanchezcloud_identity.models.user import UserRecord
from structlog.contextvars import get_contextvars

from scholight.api.deps import SearchActor, get_current_user, get_optional_search_actor
from scholight.api.history_mapper import map_search_history_page
from scholight.api.http_errors import http_error
from scholight.api.models.history import (
    BulkDeleteSearchHistoryRequest,
    BulkDeleteSearchHistoryResponse,
    PublicSearchHistoryPage,
)
from scholight.api.models.search import PublicSearchRequest, PublicSearchResponse
from scholight.api.search_execution import (
    PublicSearchError,
    SearchInvocation,
    execute_public_search,
)
from scholight.db.client import DBError
from scholight.db.queries_history import (
    bulk_soft_delete_search_entries,
    get_search_history,
    soft_delete_search_entry,
)

router = APIRouter()


@router.post(
    "",
    response_model=PublicSearchResponse,
    openapi_extra={"security": [{}]},
)
async def search(
    request: Request,
    body: PublicSearchRequest,
    actor: SearchActor | None = Depends(get_optional_search_actor),
) -> PublicSearchResponse:
    request_id = str(get_contextvars().get("request_id") or uuid4())
    try:
        return await execute_public_search(
            body,
            SearchInvocation(
                actor=actor,
                client_ip=request.client.host if request.client is not None else None,
                request_id=request_id,
                transport="rest",
            ),
        )
    except PublicSearchError as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.http_detail,
            headers=headers,
        ) from exc


@router.get("/history", response_model=PublicSearchHistoryPage)
async def list_history(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    q: Annotated[str | None, Query(max_length=200)] = None,
    current_user: UserRecord = Depends(get_current_user),
) -> PublicSearchHistoryPage:
    normalized_q = q.strip() or None if q is not None else None
    try:
        page = await get_search_history(
            current_user.id,
            limit=limit,
            offset=offset,
            q=normalized_q,
        )
    except DBError as exc:
        raise _history_unavailable() from exc
    return map_search_history_page(page, limit=limit, offset=offset)


@router.post(
    "/history/bulk-delete",
    response_model=BulkDeleteSearchHistoryResponse,
)
async def bulk_delete_history(
    body: BulkDeleteSearchHistoryRequest,
    current_user: UserRecord = Depends(get_current_user),
) -> BulkDeleteSearchHistoryResponse:
    try:
        deleted = await bulk_soft_delete_search_entries(current_user.id, body.ids)
    except DBError as exc:
        raise _history_unavailable() from exc
    return BulkDeleteSearchHistoryResponse(deleted=deleted)


@router.delete("/history/{entry_id}", response_model=MessageResponse)
async def delete_history_entry(
    entry_id: int,
    current_user: UserRecord = Depends(get_current_user),
) -> MessageResponse:
    try:
        ok = await soft_delete_search_entry(entry_id, current_user.id)
    except DBError as exc:
        raise _history_unavailable() from exc
    if not ok:
        raise http_error(
            404,
            code="history_entry_not_found",
            message="This search history entry no longer exists.",
            retryable=False,
            retry_after=None,
        )
    return MessageResponse(message="Deleted")


def _history_unavailable() -> HTTPException:
    return http_error(
        503,
        code="history_unavailable",
        message="Search history is temporarily unavailable.",
        retryable=True,
        retry_after=5,
    )
