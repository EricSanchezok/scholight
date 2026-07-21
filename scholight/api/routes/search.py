"""Search routes — semantic search, search history."""

from __future__ import annotations

import asyncio
import time

import structlog
from cloud_auth.db.queries_quota import check_and_increment_quota, decrement_quota
from cloud_auth.models.auth import MessageResponse
from cloud_auth.models.user import UserRecord
from fastapi import APIRouter, Depends, HTTPException, Request

from scholight.api.deps import get_current_user, limiter
from scholight.api.models.search import PublicSearchRequest, PublicSearchResponse
from scholight.api.search_mapper import map_search_response
from scholight.db.client import get_pool
from scholight.db.queries_history import get_search_history, log_search, soft_delete_search_entry
from scholight.models.history import SearchHistoryEntry

logger = structlog.get_logger(__name__)

_background_tasks: set[asyncio.Task[object]] = set()

router = APIRouter()


@router.post("", response_model=PublicSearchResponse)
@limiter.limit("30/minute")
async def search(
    request: Request,  # noqa: ARG001
    body: PublicSearchRequest,
    current_user: UserRecord = Depends(get_current_user),
) -> PublicSearchResponse:
    from scholight.search.engine import SearchEngine  # lazy — heavy import chain

    internal_request = body.to_internal()
    operation = f"search_level{internal_request.level}"
    quota_result = await check_and_increment_quota(get_pool, current_user.id, operation)
    if not quota_result.allowed:
        # check_and_increment_quota already incremented; roll back so the
        # rejected request does not permanently consume a daily slot.
        await decrement_quota(get_pool, current_user.id, operation)
        raise HTTPException(
            status_code=429,
            detail={
                "error": "quota_exceeded",
                "operation": operation,
                "limit": quota_result.daily_limit,
                "used": quota_result.current_count - 1,
                "message": f"Daily limit of {quota_result.daily_limit} reached for {operation}.",
            },
        )

    t_start = time.perf_counter()
    engine = SearchEngine()
    try:
        result = await engine.search(internal_request)
    except Exception as exc:
        await decrement_quota(get_pool, current_user.id, operation)
        logger.exception("search failed", query=body.query[:80])
        raise HTTPException(status_code=500, detail="Search service error") from exc

    elapsed_ms = (time.perf_counter() - t_start) * 1000

    filters: dict[str, object] = {}
    if internal_request.categories:
        filters["categories"] = internal_request.categories
    if internal_request.authors:
        filters["authors"] = internal_request.authors
    if internal_request.date_from:
        filters["date_from"] = internal_request.date_from
    if internal_request.date_to:
        filters["date_to"] = internal_request.date_to

    task = asyncio.create_task(
        log_search(
            user_id=current_user.id,
            query_text=internal_request.query,
            level=internal_request.level,
            strategy=internal_request.strategy,
            filters=filters if filters else None,
            num_results=len(result.hits),
            response_time_ms=elapsed_ms,
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return map_search_response(result, strength=body.strength, elapsed_ms=elapsed_ms)


@router.get("/history", response_model=list[SearchHistoryEntry])
async def list_history(
    limit: int = 20,
    offset: int = 0,
    current_user: UserRecord = Depends(get_current_user),
) -> list[SearchHistoryEntry]:
    return await get_search_history(current_user.id, limit=limit, offset=offset)


@router.delete("/history/{entry_id}", response_model=MessageResponse)
async def delete_history_entry(
    entry_id: int,
    current_user: UserRecord = Depends(get_current_user),
) -> MessageResponse:
    ok = await soft_delete_search_entry(entry_id, current_user.id)
    if not ok:
        raise HTTPException(status_code=404, detail="Entry not found")
    return MessageResponse(message="Deleted")
