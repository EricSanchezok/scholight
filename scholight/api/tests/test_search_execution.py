"""Transport-neutral public-search orchestration tests."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sanchezcloud_identity.models.user import UserRecord

from scholight.api.deps import SearchActor
from scholight.api.models.search import PublicSearchRequest
from scholight.api.search_access import SearchQuotaReservation
from scholight.api.search_execution import (
    SearchInvocation,
    _emit_phase_metrics,
    _failure_metric_name,
    _is_unexpected_5xx,
    _log_survey_search_finished,
    execute_public_search,
)
from scholight.models.search import PhaseTiming, SearchResult, SearchStats


def test_search_execution_has_no_transport_imports() -> None:
    module = Path(__file__).parents[1] / "search_execution.py"
    tree = ast.parse(module.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )

    assert imported.isdisjoint({"click", "fastapi", "mcp"})


def test_phase_metrics_use_fixed_metric_names() -> None:
    result = SearchResult(
        query="retrieval",
        level=2,
        total_ms=125.0,
        hits=[],
        stats=SearchStats(
            level=2,
            embedding_model="test",
            embedding_dim=2,
            paper_candidates=5,
            phases=[
                PhaseTiming(phase="embed_query", duration_ms=25.0),
                PhaseTiming(phase="paper_search", duration_ms=75.0),
                PhaseTiming(phase="rrf_fusion", duration_ms=5.0),
                PhaseTiming(phase="unknown_future_phase", duration_ms=20.0),
            ],
        ),
    )

    with patch("scholight.api.search_execution.emit_emf") as emit:
        _emit_phase_metrics(result, strength="thorough")

    emit.assert_called_once_with(
        service="api",
        strength="thorough",
        metrics={
            "StageEmbedQueryLatency": (25.0, "Milliseconds"),
            "StagePaperSearchLatency": (75.0, "Milliseconds"),
            "StageRrfFusionLatency": (5.0, "Milliseconds"),
        },
    )


@pytest.mark.parametrize(
    ("error", "metric"),
    [
        (TimeoutError(), "SearchTimeout"),
        (ConnectionResetError(), "SearchConnectionReset"),
        (ConnectionRefusedError(), "SearchConnectError"),
        (RuntimeError(), "SearchDependencyFailure"),
    ],
)
def test_failure_metric_classification_is_low_cardinality(
    error: BaseException,
    metric: str,
) -> None:
    assert _failure_metric_name(error) == metric


def test_expected_dependency_503_is_not_counted_as_unexpected() -> None:
    assert _is_unexpected_5xx(503, "search_unavailable") is False
    assert _is_unexpected_5xx(503, "thorough_search_unavailable") is False
    assert _is_unexpected_5xx(500, "search_cancelled") is True


def test_survey_search_log_has_job_correlation_but_no_query(active_user: UserRecord) -> None:
    job_id = uuid4()
    invocation = SearchInvocation(
        actor=SearchActor(
            user=active_user,
            actor_type="delegated",
            survey_job_id=job_id,
        ),
        client_ip="192.0.2.20",
        request_id="request-1",
        transport="mcp",
    )

    with patch("scholight.api.search_execution.logger") as logger:
        _log_survey_search_finished(
            invocation,
            strength="standard",
            outcome="success",
            status_code=200,
            duration_ms=12.5,
            result_count=5,
            error_code=None,
        )

    fields = logger.info.call_args.kwargs
    assert fields["survey_job_id"] == str(job_id)
    assert fields["request_id"] == "request-1"
    assert "query" not in fields


@pytest.mark.asyncio
async def test_execute_public_search_uses_invocation_client_ip() -> None:
    reservation = SearchQuotaReservation(strength="standard")
    result = SearchResult(query="retrieval", level=1, total_ms=1.0, hits=[])

    with (
        patch(
            "scholight.api.search_execution.reserve_search_quota",
            new_callable=AsyncMock,
            return_value=reservation,
        ) as reserve,
        patch(
            "scholight.search.engine.SearchEngine.search",
            new_callable=AsyncMock,
            return_value=result,
        ),
    ):
        response = await execute_public_search(
            PublicSearchRequest(query="retrieval"),
            SearchInvocation(
                actor=None,
                client_ip="192.0.2.20",
                request_id="request-1",
                transport="rest",
            ),
        )

    assert response.result_count == 0
    reserve.assert_awaited_once_with("192.0.2.20", None, strength="standard")
