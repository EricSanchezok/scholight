"""Search routes — semantic search, search history."""

from __future__ import annotations

import asyncio
import time
from typing import Annotated

import grpc
import structlog
from cloud_auth.models.auth import MessageResponse
from cloud_auth.models.user import UserRecord
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pymilvus.exceptions import MilvusException
from structlog.contextvars import get_contextvars

from scholight.api.deps import get_current_user, get_optional_current_user
from scholight.api.history_mapper import map_search_history_page
from scholight.api.history_tasks import schedule_search_history_write
from scholight.api.models.history import (
    BulkDeleteSearchHistoryRequest,
    BulkDeleteSearchHistoryResponse,
    PublicSearchHistoryPage,
)
from scholight.api.models.search import PublicSearchRequest, PublicSearchResponse
from scholight.api.search_access import compensate_search_quota, reserve_search_quota
from scholight.api.search_mapper import map_search_response
from scholight.config import settings
from scholight.db.client import DBError
from scholight.db.queries_history import (
    bulk_soft_delete_search_entries,
    get_search_history,
    soft_delete_search_entry,
)
from scholight.search.errors import SearchUnavailable, ThoroughSearchUnavailable
from scholight.store.ingest import StoreError
from scholight.store.query import batch_get_arxiv_papers

logger = structlog.get_logger(__name__)

_PUBLIC_ENRICHMENT_FIELDS = ["arxiv_id", "abstract"]


async def _enrich_public_abstracts(
    result: object,
) -> tuple[dict[str, str | None], bool]:
    from scholight.models.search import SearchResult

    if not isinstance(result, SearchResult):
        raise TypeError("result must be a SearchResult")
    if not result.hits:
        return {}, False

    arxiv_ids = [hit.arxiv_id for hit in result.hits]
    try:
        papers = await asyncio.to_thread(
            batch_get_arxiv_papers,
            arxiv_ids,
            output_fields=_PUBLIC_ENRICHMENT_FIELDS,
            timeout=settings.search_enrichment_rpc_timeout_seconds,
        )
    except (grpc.RpcError, MilvusException, StoreError, OSError, TimeoutError) as exc:
        logger.warning("public_search_enrichment_failed", error_type=type(exc).__name__)
        return {}, True

    abstracts: dict[str, str | None] = {}
    degraded = False
    for arxiv_id in arxiv_ids:
        if arxiv_id not in papers:
            degraded = True
            continue
        paper = papers[arxiv_id]
        if not isinstance(paper, dict):
            raise TypeError("enrichment rows must be mappings")
        abstract = paper.get("abstract")
        if not isinstance(abstract, str) or not abstract.strip():
            abstracts[arxiv_id] = None
            degraded = True
            continue
        abstracts[arxiv_id] = abstract
    return abstracts, degraded


router = APIRouter()


@router.post(
    "",
    response_model=PublicSearchResponse,
    openapi_extra={"security": [{}]},
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

    try:
        abstracts, degraded = await _enrich_public_abstracts(result)
        elapsed_ms = (time.perf_counter() - t_start) * 1000
        response = map_search_response(
            result,
            strength=body.strength,
            elapsed_ms=elapsed_ms,
            degraded=degraded,
            abstracts=abstracts,
        )
    except Exception as exc:
        logger.exception("search_post_commit_failed", strength=body.strength)
        raise HTTPException(status_code=500, detail="Search service error") from exc

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
        request_id = str(get_contextvars().get("request_id", ""))
        schedule_search_history_write(
            request_id=request_id,
            user_id=current_user.id,
            query_text=internal_request.query,
            level=internal_request.level,
            strength=body.strength.value,
            filters=filters if filters else None,
            result_count=len(result.hits),
            elapsed_ms=elapsed_ms,
        )

    return response


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
        raise HTTPException(status_code=404, detail="Entry not found")
    return MessageResponse(message="Deleted")


def _history_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": "history_unavailable",
            "message": "Search history is temporarily unavailable.",
            "retryable": True,
        },
        headers={"Retry-After": "5"},
    )
