"""Search routes — semantic search, search history."""

from __future__ import annotations

import asyncio
import time

import structlog
from cloud_auth.models.auth import MessageResponse
from cloud_auth.models.user import UserRecord
from fastapi import APIRouter, Depends, HTTPException, Request

from scholight.api.deps import get_current_user, get_optional_current_user
from scholight.api.models.search import PublicSearchRequest, PublicSearchResponse
from scholight.api.search_access import compensate_search_quota, reserve_search_quota
from scholight.api.search_mapper import map_search_response
from scholight.db.queries_history import get_search_history, log_search, soft_delete_search_entry
from scholight.models.history import SearchHistoryEntry
from scholight.search.errors import SearchUnavailable, ThoroughSearchUnavailable

logger = structlog.get_logger(__name__)

_background_tasks: set[asyncio.Task[object]] = set()

router = APIRouter()


@router.post(
    "",
    response_model=PublicSearchResponse,
    openapi_extra={"security": [{"BearerAuth": []}, {}]},
)
async def search(
    request: Request,
    body: PublicSearchRequest,
    current_user: UserRecord | None = Depends(get_optional_current_user),
) -> PublicSearchResponse:
    from scholight.search.engine import SearchEngine  # lazy — heavy import chain

    internal_request = body.to_internal()
    reservation = await reserve_search_quota(
        request,
        current_user,
        search_level=internal_request.level,
    )

    t_start = time.perf_counter()
    engine = SearchEngine()
    try:
        result = await engine.search(internal_request)
    except asyncio.CancelledError as exc:
        await compensate_search_quota(reservation)
        logger.error("search_cancelled", strength=body.strength)
        raise HTTPException(status_code=500, detail="Search service error") from exc
    except ThoroughSearchUnavailable as exc:
        await compensate_search_quota(reservation)
        logger.warning(
            "thorough_search_unavailable",
            strength=body.strength,
            phase=exc.phase_name,
            error_type=type(exc.cause).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "thorough_search_unavailable",
                "message": "Thorough search is temporarily unavailable.",
                "retryable": True,
            },
            headers={"Retry-After": "5"},
        ) from exc
    except SearchUnavailable as exc:
        await compensate_search_quota(reservation)
        logger.warning(
            "search_unavailable",
            strength=body.strength,
            phase=exc.phase_name,
            error_type=type(exc.cause).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "search_unavailable",
                "message": "Search is temporarily unavailable.",
                "retryable": True,
            },
            headers={"Retry-After": "5"},
        ) from exc
    except Exception as exc:
        await compensate_search_quota(reservation)
        logger.exception("search_failed", strength=body.strength)
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

    if current_user is not None:
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
