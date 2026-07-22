"""Final public search enrichment and post-commit mapping tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import httpx
import pytest
from pymilvus.exceptions import MilvusException

from scholight.api.search_access import SearchQuotaReservation
from scholight.models.search import SearchHit, SearchResult
from scholight.store import StoreError


def _hit(rank: int, arxiv_id: str, *, score: float) -> SearchHit:
    return SearchHit(
        rank=rank,
        score=score,
        arxiv_id=arxiv_id,
        title=f"Paper {arxiv_id}",
        authors=["Author"],
        abstract="",
        categories=["cs.AI"],
        created="2024-01-01",
        updated="2024-02-01",
        version=1,
        updated_history=[],
        license="",
        comments="",
        doi="",
        journal_ref="",
        acm_class="",
    )


def _result(*hits: SearchHit) -> SearchResult:
    return SearchResult(query="retrieval", level=1, total_ms=10.0, hits=list(hits))


@pytest.mark.asyncio
async def test_final_enrichment_runs_one_batch_and_preserves_core_rank(
    api_client: httpx.AsyncClient,
) -> None:
    reservation = SearchQuotaReservation(operation="search_level1")
    core_result = _result(_hit(1, "B", score=1.0), _hit(2, "A", score=0.9))
    enrichment = MagicMock(
        return_value={
            "A": {"arxiv_id": "A", "abstract": "Abstract A"},
            "B": {"arxiv_id": "B", "abstract": "Abstract B"},
        }
    )

    with (
        patch(
            "scholight.api.routes.search.reserve_search_quota",
            new_callable=AsyncMock,
            return_value=reservation,
        ),
        patch(
            "scholight.search.engine.SearchEngine.search",
            new_callable=AsyncMock,
            return_value=core_result,
        ),
        patch("scholight.api.routes.search.batch_get_arxiv_papers", enrichment),
        patch(
            "scholight.api.routes.search.compensate_search_quota",
            new_callable=AsyncMock,
        ) as compensate,
    ):
        response = await api_client.post("/search", json={"query": "retrieval", "limit": 2})

    body = response.json()
    assert (response.status_code, body["degraded"]) == (200, False)
    assert [hit["arxiv_id"] for hit in body["hits"]] == ["B", "A"]
    assert [hit["abstract"] for hit in body["hits"]] == ["Abstract B", "Abstract A"]
    assert enrichment.call_count == 1
    assert enrichment.call_args.args == (["B", "A"],)
    assert enrichment.call_args.kwargs["output_fields"] == ["arxiv_id", "abstract"]
    assert enrichment.call_args.kwargs["timeout"] == 1.5
    compensate.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_enrichment_row_is_degraded_with_null_abstract(
    api_client: httpx.AsyncClient,
) -> None:
    reservation = SearchQuotaReservation(operation="search_level1")
    core_result = _result(_hit(1, "A", score=1.0), _hit(2, "B", score=0.9))

    with (
        patch(
            "scholight.api.routes.search.reserve_search_quota",
            new_callable=AsyncMock,
            return_value=reservation,
        ),
        patch(
            "scholight.search.engine.SearchEngine.search",
            new_callable=AsyncMock,
            return_value=core_result,
        ),
        patch(
            "scholight.api.routes.search.batch_get_arxiv_papers",
            return_value={"A": {"arxiv_id": "A", "abstract": "Abstract A"}},
        ),
        patch(
            "scholight.api.routes.search.compensate_search_quota",
            new_callable=AsyncMock,
        ) as compensate,
    ):
        response = await api_client.post("/search", json={"query": "retrieval", "limit": 2})

    body = response.json()
    assert (response.status_code, body["degraded"]) == (200, True)
    assert [hit["abstract"] for hit in body["hits"]] == ["Abstract A", None]
    compensate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError(),
        OSError("connection reset"),
        MilvusException(message="private endpoint unavailable", code=1),
        StoreError("private store detail"),
        grpc.RpcError("deadline exceeded"),
    ],
)
async def test_known_enrichment_failure_returns_degraded_without_compensation(
    api_client: httpx.AsyncClient, failure: Exception
) -> None:
    reservation = SearchQuotaReservation(operation="search_level1")
    core_result = _result(_hit(1, "A", score=1.0))

    with (
        patch(
            "scholight.api.routes.search.reserve_search_quota",
            new_callable=AsyncMock,
            return_value=reservation,
        ),
        patch(
            "scholight.search.engine.SearchEngine.search",
            new_callable=AsyncMock,
            return_value=core_result,
        ),
        patch(
            "scholight.api.routes.search.batch_get_arxiv_papers",
            side_effect=failure,
        ),
        patch(
            "scholight.api.routes.search.compensate_search_quota",
            new_callable=AsyncMock,
        ) as compensate,
    ):
        response = await api_client.post("/search", json={"query": "retrieval"})

    assert (response.status_code, response.json()["degraded"]) == (200, True)
    assert response.json()["hits"][0]["abstract"] is None
    compensate.assert_not_awaited()


@pytest.mark.asyncio
async def test_zero_core_hits_skip_final_enrichment(
    api_client: httpx.AsyncClient,
) -> None:
    reservation = SearchQuotaReservation(operation="search_level1")
    enrichment = MagicMock()

    with (
        patch(
            "scholight.api.routes.search.reserve_search_quota",
            new_callable=AsyncMock,
            return_value=reservation,
        ),
        patch(
            "scholight.search.engine.SearchEngine.search",
            new_callable=AsyncMock,
            return_value=_result(),
        ),
        patch("scholight.api.routes.search.batch_get_arxiv_papers", enrichment),
    ):
        response = await api_client.post("/search", json={"query": "retrieval"})

    assert (response.status_code, response.json()["hits"], response.json()["degraded"]) == (
        200,
        [],
        False,
    )
    enrichment.assert_not_called()


@pytest.mark.asyncio
async def test_post_commit_enrichment_program_error_returns_500_with_compensation(
    api_client: httpx.AsyncClient,
) -> None:
    reservation = SearchQuotaReservation(operation="search_level1")
    core_result = _result(_hit(1, "A", score=1.0))

    with (
        patch(
            "scholight.api.routes.search.reserve_search_quota",
            new_callable=AsyncMock,
            return_value=reservation,
        ),
        patch(
            "scholight.search.engine.SearchEngine.search",
            new_callable=AsyncMock,
            return_value=core_result,
        ),
        patch(
            "scholight.api.routes.search.batch_get_arxiv_papers",
            return_value={"A": None},
        ),
        patch(
            "scholight.api.routes.search.compensate_search_quota",
            new_callable=AsyncMock,
        ) as compensate,
        patch(
            "scholight.api.routes.search.schedule_search_history_write",
        ) as schedule_history,
    ):
        response = await api_client.post("/search", json={"query": "retrieval"})

    assert (response.status_code, response.json()) == (
        500,
        {"detail": "Search service error"},
    )
    compensate.assert_awaited_once()
    schedule_history.assert_not_called()


@pytest.mark.asyncio
async def test_post_commit_mapper_error_returns_500_with_compensation(
    api_client: httpx.AsyncClient,
) -> None:
    reservation = SearchQuotaReservation(operation="search_level1")
    core_result = _result(_hit(1, "A", score=1.0))

    with (
        patch(
            "scholight.api.routes.search.reserve_search_quota",
            new_callable=AsyncMock,
            return_value=reservation,
        ),
        patch(
            "scholight.search.engine.SearchEngine.search",
            new_callable=AsyncMock,
            return_value=core_result,
        ),
        patch(
            "scholight.api.routes.search.batch_get_arxiv_papers",
            return_value={"A": {"arxiv_id": "A", "abstract": "Abstract A"}},
        ),
        patch("scholight.api.routes.search.map_search_response", side_effect=ValueError("bug")),
        patch(
            "scholight.api.routes.search.compensate_search_quota",
            new_callable=AsyncMock,
        ) as compensate,
    ):
        response = await api_client.post("/search", json={"query": "retrieval"})

    assert (response.status_code, response.json()) == (
        500,
        {"detail": "Search service error"},
    )
    compensate.assert_awaited_once()
