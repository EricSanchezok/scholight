"""Transport-neutral orchestration for the public Scholight search contract."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

import grpc
import structlog
from cloud_auth.models.user import UserRecord
from pymilvus.exceptions import MilvusException

from scholight.api.history_tasks import schedule_search_history_write
from scholight.api.models.search import PublicSearchRequest, PublicSearchResponse
from scholight.api.search_access import (
    SearchAccessError,
    compensate_search_quota,
    reserve_search_quota,
)
from scholight.api.search_mapper import map_search_response
from scholight.api.usage_tasks import schedule_usage_event
from scholight.config import settings
from scholight.db.queries_usage import UsageEvent
from scholight.models.search import SearchResult
from scholight.search.errors import SearchUnavailable, ThoroughSearchUnavailable
from scholight.store.ingest import StoreError
from scholight.store.query import batch_get_arxiv_papers

logger = structlog.get_logger(__name__)
_PUBLIC_ENRICHMENT_FIELDS = ["arxiv_id", "abstract"]


class _SearchActor(Protocol):
    @property
    def user(self) -> UserRecord: ...

    @property
    def actor_type(self) -> Literal["web", "access_key"]: ...

    @property
    def access_key_id(self) -> UUID | None: ...


@dataclass(frozen=True, slots=True)
class SearchInvocation:
    """Identity and network context supplied by a transport adapter."""

    actor: _SearchActor | None
    client_ip: str | None
    request_id: str


class PublicSearchError(Exception):
    """Stable public-search failure that adapters can map to their transport."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool,
        retry_after: int | None = None,
        structured_http_detail: bool = True,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after
        self.structured_http_detail = structured_http_detail

    @property
    def http_detail(self) -> str | dict[str, object]:
        if not self.structured_http_detail:
            return self.message
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


def _schedule_usage(
    actor: _SearchActor | None,
    *,
    request_id: str,
    strength: Literal["standard", "thorough"],
    outcome: Literal["success", "degraded", "failed"],
    quota_units: int,
    result_count: int | None,
    duration_ms: float | None,
    status_code: int,
    error_code: str | None,
) -> None:
    if actor is None:
        return
    schedule_usage_event(
        UsageEvent(
            request_id=request_id,
            user_id=actor.user.id,
            strength=strength,
            actor_type=actor.actor_type,
            access_key_id=actor.access_key_id,
            outcome=outcome,
            quota_units=quota_units,
            result_count=result_count,
            search_duration_ms=duration_ms,
            status_code=status_code,
            error_code=error_code,
        )
    )


async def _enrich_public_abstracts(result: SearchResult) -> tuple[dict[str, str | None], bool]:
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


def _execution_error(
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool,
    retry_after: int | None = None,
    structured_http_detail: bool = True,
) -> PublicSearchError:
    return PublicSearchError(
        status_code=status_code,
        code=code,
        message=message,
        retryable=retryable,
        retry_after=retry_after,
        structured_http_detail=structured_http_detail,
    )


async def execute_public_search(
    body: PublicSearchRequest,
    invocation: SearchInvocation,
) -> PublicSearchResponse:
    """Run one public search with quota, enrichment, history, and usage side effects."""
    from scholight.search.engine import SearchEngine

    internal_request = body.to_internal()
    current_user = invocation.actor.user if invocation.actor is not None else None
    try:
        reservation = await reserve_search_quota(
            invocation.client_ip,
            current_user,
            strength=body.strength.value,
        )
    except SearchAccessError as exc:
        raise _execution_error(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            retryable=True,
            retry_after=exc.retry_after,
        ) from exc

    t_start = time.perf_counter()
    engine = SearchEngine()
    try:
        result = await engine.search(internal_request)
    except asyncio.CancelledError as exc:
        await compensate_search_quota(reservation)
        elapsed_ms = (time.perf_counter() - t_start) * 1000
        _schedule_usage(
            invocation.actor,
            request_id=invocation.request_id,
            strength=body.strength.value,
            outcome="failed",
            quota_units=0,
            result_count=None,
            duration_ms=elapsed_ms,
            status_code=500,
            error_code="search_cancelled",
        )
        logger.error("search_cancelled", strength=body.strength)
        raise _execution_error(
            status_code=500,
            code="search_cancelled",
            message="Search service error",
            retryable=False,
            structured_http_detail=False,
        ) from exc
    except ThoroughSearchUnavailable as exc:
        await compensate_search_quota(reservation)
        elapsed_ms = (time.perf_counter() - t_start) * 1000
        _schedule_usage(
            invocation.actor,
            request_id=invocation.request_id,
            strength=body.strength.value,
            outcome="failed",
            quota_units=0,
            result_count=None,
            duration_ms=elapsed_ms,
            status_code=503,
            error_code="thorough_search_unavailable",
        )
        logger.warning(
            "thorough_search_unavailable",
            strength=body.strength,
            phase=exc.phase_name,
            error_type=type(exc.cause).__name__,
        )
        raise _execution_error(
            status_code=503,
            code="thorough_search_unavailable",
            message="Thorough search is temporarily unavailable.",
            retryable=True,
            retry_after=5,
        ) from exc
    except SearchUnavailable as exc:
        await compensate_search_quota(reservation)
        elapsed_ms = (time.perf_counter() - t_start) * 1000
        _schedule_usage(
            invocation.actor,
            request_id=invocation.request_id,
            strength=body.strength.value,
            outcome="failed",
            quota_units=0,
            result_count=None,
            duration_ms=elapsed_ms,
            status_code=503,
            error_code="search_unavailable",
        )
        logger.warning(
            "search_unavailable",
            strength=body.strength,
            phase=exc.phase_name,
            error_type=type(exc.cause).__name__,
        )
        raise _execution_error(
            status_code=503,
            code="search_unavailable",
            message="Search is temporarily unavailable.",
            retryable=True,
            retry_after=5,
        ) from exc
    except Exception as exc:
        await compensate_search_quota(reservation)
        elapsed_ms = (time.perf_counter() - t_start) * 1000
        _schedule_usage(
            invocation.actor,
            request_id=invocation.request_id,
            strength=body.strength.value,
            outcome="failed",
            quota_units=0,
            result_count=None,
            duration_ms=elapsed_ms,
            status_code=500,
            error_code="search_failed",
        )
        logger.exception("search_failed", strength=body.strength)
        raise _execution_error(
            status_code=500,
            code="search_failed",
            message="Search service error",
            retryable=False,
            structured_http_detail=False,
        ) from exc

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
        await compensate_search_quota(reservation)
        elapsed_ms = (time.perf_counter() - t_start) * 1000
        _schedule_usage(
            invocation.actor,
            request_id=invocation.request_id,
            strength=body.strength.value,
            outcome="failed",
            quota_units=0,
            result_count=None,
            duration_ms=elapsed_ms,
            status_code=500,
            error_code="search_post_commit_failed",
        )
        logger.exception("search_post_commit_failed", strength=body.strength)
        raise _execution_error(
            status_code=500,
            code="search_post_commit_failed",
            message="Search service error",
            retryable=False,
            structured_http_detail=False,
        ) from exc

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
        schedule_search_history_write(
            request_id=invocation.request_id,
            user_id=current_user.id,
            query_text=internal_request.query,
            strength=body.strength.value,
            filters=filters if filters else None,
            result_count=len(result.hits),
            elapsed_ms=elapsed_ms,
        )
        _schedule_usage(
            invocation.actor,
            request_id=invocation.request_id,
            strength=body.strength.value,
            outcome="degraded" if degraded else "success",
            quota_units=1,
            result_count=len(result.hits),
            duration_ms=elapsed_ms,
            status_code=200,
            error_code=None,
        )

    return response


__all__ = ["PublicSearchError", "SearchInvocation", "execute_public_search"]
