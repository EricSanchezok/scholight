"""Transport-neutral orchestration for the public Scholight search contract."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

import grpc
import structlog
from pymilvus.exceptions import MilvusException
from sanchezcloud_identity.models.user import UserRecord

from scholight.api.history_tasks import schedule_search_history_write
from scholight.api.models.search import PublicSearchRequest, PublicSearchResponse
from scholight.api.search_access import (
    SearchAccessError,
    compensate_search_quota,
    reserve_search_quota,
)
from scholight.api.search_in_flight import get_search_in_flight_tracker
from scholight.api.search_mapper import map_search_response
from scholight.api.usage_tasks import schedule_usage_event
from scholight.config import settings
from scholight.db.queries_usage import UsageEvent
from scholight.logging.emf import MetricUnit, emit_emf
from scholight.models.quota import QuotaErrorDetails
from scholight.models.search import SearchResult
from scholight.search.errors import SearchUnavailable, ThoroughSearchUnavailable
from scholight.search.executor import run_search_blocking
from scholight.store.ingest import StoreError
from scholight.store.query import batch_get_arxiv_papers

logger = structlog.get_logger(__name__)
_PUBLIC_ENRICHMENT_FIELDS = ["arxiv_id", "abstract"]
_PHASE_METRIC_NAMES = {
    "embed_query": "StageEmbedQueryLatency",
    "paper_search": "StagePaperSearchLatency",
    "score_fusion": "StageScoreFusionLatency",
    "chunk_search": "StageChunkSearchLatency",
    "chunk_aggregation": "StageChunkAggregationLatency",
    "rrf_fusion": "StageRrfFusionLatency",
}
_EXPECTED_SERVER_ERROR_CODES = frozenset(
    {
        "quota_service_unavailable",
        "search_unavailable",
        "thorough_search_unavailable",
    }
)


class _SearchActor(Protocol):
    @property
    def user(self) -> UserRecord: ...

    @property
    def actor_type(self) -> Literal["web", "access_key", "delegated"]: ...

    @property
    def access_key_id(self) -> UUID | None: ...


@dataclass(frozen=True, slots=True)
class SearchInvocation:
    """Identity and network context supplied by a transport adapter."""

    actor: _SearchActor | None
    client_ip: str | None
    request_id: str
    transport: Literal["rest", "mcp"]


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
        quota: QuotaErrorDetails | dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after
        self.quota = QuotaErrorDetails.model_validate(quota) if quota is not None else None

    @property
    def http_detail(self) -> dict[str, object]:
        detail: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.quota is not None:
            detail["quota"] = self.quota.model_dump(mode="json")
        return detail


def _schedule_usage(
    actor: _SearchActor | None,
    *,
    transport: Literal["rest", "mcp"],
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
            transport=transport,
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
        papers = await run_search_blocking(
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
    quota: QuotaErrorDetails | None = None,
) -> PublicSearchError:
    return PublicSearchError(
        status_code=status_code,
        code=code,
        message=message,
        retryable=retryable,
        retry_after=retry_after,
        quota=quota,
    )


def _emit_phase_metrics(result: SearchResult, *, strength: str) -> None:
    """Emit only the fixed, low-cardinality search phase vocabulary."""
    if result.stats is None:
        return
    metrics: dict[str, tuple[float | int, MetricUnit]] = {
        metric_name: (phase.duration_ms, "Milliseconds")
        for phase in result.stats.phases
        if (metric_name := _PHASE_METRIC_NAMES.get(phase.phase)) is not None
    }
    if metrics:
        emit_emf(service="api", strength=strength, metrics=metrics)


def _iter_exception_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _failure_metric_name(error: BaseException) -> str:
    """Classify dependency failures without emitting exception text or identifiers."""
    chain = _iter_exception_chain(error)
    if any(isinstance(item, TimeoutError) for item in chain):
        return "SearchTimeout"
    if any(isinstance(item, (ConnectionResetError, BrokenPipeError)) for item in chain):
        return "SearchConnectionReset"
    if any(isinstance(item, (ConnectionRefusedError, ConnectionError, OSError)) for item in chain):
        return "SearchConnectError"
    return "SearchDependencyFailure"


def _emit_failure_metric(error: BaseException, *, strength: str) -> None:
    emit_emf(
        service="api",
        strength=strength,
        metrics={_failure_metric_name(error): (1, "Count")},
    )


def _is_unexpected_5xx(status_code: int, code: str) -> bool:
    return status_code >= 500 and code not in _EXPECTED_SERVER_ERROR_CODES


def _log_survey_search_finished(
    invocation: SearchInvocation,
    *,
    strength: str,
    outcome: str,
    status_code: int,
    duration_ms: float,
    result_count: int | None,
    error_code: str | None,
) -> None:
    """Correlate delegated Survey searches without logging queries or model content."""
    survey_job_id = getattr(invocation.actor, "survey_job_id", None)
    if survey_job_id is None:
        return
    logger.info(
        "survey_search_finished",
        survey_job_id=str(survey_job_id),
        request_id=invocation.request_id,
        strength=strength,
        outcome=outcome,
        status_code=status_code,
        duration_ms=round(duration_ms, 3),
        result_count=result_count,
        error_code=error_code,
    )


def _emit_in_flight_metric(body: PublicSearchRequest, invocation: SearchInvocation) -> None:
    snapshot = get_search_in_flight_tracker().snapshot()
    try:
        emit_emf(
            service="api",
            strength=body.strength.value,
            transport=invocation.transport,
            metrics={"InFlight": (snapshot.total, "Count")},
        )
        emit_emf(
            service="api",
            metrics={"InFlight": (snapshot.total, "Count")},
        )
    except Exception:
        logger.warning("search_in_flight_metric_emit_failed", strength=body.strength.value)


def _emit_search_metrics(
    body: PublicSearchRequest,
    invocation: SearchInvocation,
    *,
    outcome: str,
    elapsed_ms: float,
    unexpected_5xx: bool = False,
    in_flight: int | None = None,
) -> None:
    metrics: dict[str, tuple[float | int, Literal["Count", "Milliseconds"]]] = {
        "RequestCount": (1, "Count")
    }
    if unexpected_5xx:
        metrics["Unexpected5xx"] = (1, "Count")
    if in_flight is not None:
        metrics["InFlight"] = (in_flight, "Count")
    try:
        emit_emf(
            service="api",
            strength=body.strength.value,
            transport=invocation.transport,
            outcome=outcome,
            metrics=metrics,
        )
        aggregate_metrics: dict[str, tuple[float | int, MetricUnit]] = {
            "RequestCount": (1, "Count")
        }
        if unexpected_5xx:
            aggregate_metrics["Unexpected5xx"] = (1, "Count")
        if in_flight is not None:
            aggregate_metrics["InFlight"] = (in_flight, "Count")
        emit_emf(service="api", metrics=aggregate_metrics)
        emit_emf(
            service="api",
            strength=body.strength.value,
            metrics={
                ("SuccessLatency" if outcome in {"success", "degraded"} else "ErrorLatency"): (
                    elapsed_ms,
                    "Milliseconds",
                )
            },
        )
    except Exception:
        logger.warning("search_metric_emit_failed", strength=body.strength.value)


async def _execute_search(
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
            quota=exc.quota,
        ) from exc

    t_start = time.perf_counter()
    engine = SearchEngine()
    try:
        result = await engine.search(internal_request)
        _emit_phase_metrics(result, strength=body.strength.value)
    except asyncio.CancelledError as exc:
        await compensate_search_quota(reservation)
        elapsed_ms = (time.perf_counter() - t_start) * 1000
        _schedule_usage(
            invocation.actor,
            transport=invocation.transport,
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
            code="search_failed",
            message="The search could not be completed because of an unexpected service error.",
            retryable=False,
        ) from exc
    except TimeoutError as exc:
        _emit_failure_metric(exc, strength=body.strength.value)
        await compensate_search_quota(reservation)
        elapsed_ms = (time.perf_counter() - t_start) * 1000
        error_code = (
            "thorough_search_unavailable"
            if body.strength.value == "thorough"
            else "search_unavailable"
        )
        _schedule_usage(
            invocation.actor,
            transport=invocation.transport,
            request_id=invocation.request_id,
            strength=body.strength.value,
            outcome="failed",
            quota_units=0,
            result_count=None,
            duration_ms=elapsed_ms,
            status_code=503,
            error_code=error_code,
        )
        logger.warning("search_timeout", strength=body.strength)
        raise _execution_error(
            status_code=503,
            code=error_code,
            message=(
                "Thorough search is temporarily unavailable."
                if body.strength.value == "thorough"
                else "Search is temporarily unavailable."
            ),
            retryable=True,
            retry_after=5,
        ) from exc
    except ThoroughSearchUnavailable as exc:
        _emit_failure_metric(exc.cause, strength=body.strength.value)
        await compensate_search_quota(reservation)
        elapsed_ms = (time.perf_counter() - t_start) * 1000
        _schedule_usage(
            invocation.actor,
            transport=invocation.transport,
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
        _emit_failure_metric(exc.cause, strength=body.strength.value)
        await compensate_search_quota(reservation)
        elapsed_ms = (time.perf_counter() - t_start) * 1000
        _schedule_usage(
            invocation.actor,
            transport=invocation.transport,
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
        _emit_failure_metric(exc, strength=body.strength.value)
        await compensate_search_quota(reservation)
        elapsed_ms = (time.perf_counter() - t_start) * 1000
        _schedule_usage(
            invocation.actor,
            transport=invocation.transport,
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
            message="The search could not be completed because of an unexpected service error.",
            retryable=False,
        ) from exc

    try:
        enrichment_started = time.perf_counter()
        abstracts, degraded = await _enrich_public_abstracts(result)
        emit_emf(
            service="api",
            strength=body.strength.value,
            metrics={
                "StageEnrichmentLatency": (
                    (time.perf_counter() - enrichment_started) * 1000,
                    "Milliseconds",
                )
            },
        )
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
            transport=invocation.transport,
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
            code="search_failed",
            message="The search could not be completed because of an unexpected service error.",
            retryable=False,
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
            transport=invocation.transport,
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


async def execute_public_search(
    body: PublicSearchRequest,
    invocation: SearchInvocation,
) -> PublicSearchResponse:
    """Observe in-flight work and execute one search without admission control."""
    started = time.perf_counter()
    tracker = get_search_in_flight_tracker()
    try:
        async with tracker.track(body.strength.value):
            _emit_in_flight_metric(body, invocation)
            try:
                response = await _execute_search(body, invocation)
            except PublicSearchError as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000
                snapshot = tracker.snapshot()
                _emit_search_metrics(
                    body,
                    invocation,
                    outcome="failed",
                    elapsed_ms=elapsed_ms,
                    unexpected_5xx=_is_unexpected_5xx(exc.status_code, exc.code),
                    in_flight=snapshot.total,
                )
                _log_survey_search_finished(
                    invocation,
                    strength=body.strength.value,
                    outcome="failed",
                    status_code=exc.status_code,
                    duration_ms=elapsed_ms,
                    result_count=None,
                    error_code=exc.code,
                )
                raise
            snapshot = tracker.snapshot()
            elapsed_ms = (time.perf_counter() - started) * 1000
            outcome = "degraded" if response.degraded else "success"
            _emit_search_metrics(
                body,
                invocation,
                outcome=outcome,
                elapsed_ms=elapsed_ms,
                in_flight=snapshot.total,
            )
            _log_survey_search_finished(
                invocation,
                strength=body.strength.value,
                outcome=outcome,
                status_code=200,
                duration_ms=elapsed_ms,
                result_count=response.result_count,
                error_code=None,
            )
            return response
    finally:
        _emit_in_flight_metric(body, invocation)


__all__ = ["PublicSearchError", "SearchInvocation", "execute_public_search"]
