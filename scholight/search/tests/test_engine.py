"""Unit tests for strict SearchEngine execution and result invariants."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import httpx
import pytest
from pymilvus.exceptions import MilvusException

from scholight.config import Settings
from scholight.models.search import SearchRequest
from scholight.search import engine as engine_module
from scholight.search.base import PhaseError, PipelineContext
from scholight.search.engine import SearchEngine
from scholight.search.errors import (
    SearchUnavailable,
    ThoroughSearchUnavailable,
)


def _paper(arxiv_id: str, score: float, *, title: str | None = None) -> dict[str, Any]:
    return {
        "arxiv_id": arxiv_id,
        "score": score,
        "title": title or f"Paper {arxiv_id}",
        "authors": ["Author"],
        "categories": ["cs.AI"],
        "created": "2024-01-01",
        "updated": "2024-02-01",
        "version": 1,
    }


def _level1_context(request: SearchRequest) -> PipelineContext:
    return PipelineContext(
        request=request,
        query_vector=[0.0] * 1024,
        raw_hits=[_paper("A", 0.9, title="Level 1")],
    )


class StubLevel1Pipeline:
    phases: ClassVar[list[SimpleNamespace]] = [
        SimpleNamespace(name="embed_query"),
        SimpleNamespace(name="paper_search"),
    ]

    def __init__(self, context: PipelineContext) -> None:
        self.context = context

    async def run(self, _request: SearchRequest) -> PipelineContext:
        return self.context


class FailingLevel1Pipeline:
    phases: ClassVar[list[SimpleNamespace]] = [SimpleNamespace(name="paper_search")]

    def __init__(self, cause: Exception) -> None:
        self.cause = cause

    async def run(self, _request: SearchRequest) -> PipelineContext:
        raise PhaseError("paper_search", self.cause)


class SlowLevel2Pipeline:
    phases: ClassVar[list[object]] = []

    async def run(
        self, _request: SearchRequest, ctx: PipelineContext | None = None
    ) -> PipelineContext:
        assert ctx is not None
        ctx.raw_hits[0]["score"] = 0.1
        ctx.raw_hits.append({"arxiv_id": "B", "score": 1.0, "title": "Level 2"})
        await asyncio.sleep(1)
        return ctx


class FailingLevel2Pipeline:
    phases: ClassVar[list[object]] = []

    def __init__(self, cause: Exception) -> None:
        self.cause = cause

    async def run(
        self, _request: SearchRequest, ctx: PipelineContext | None = None
    ) -> PipelineContext:
        assert ctx is not None
        ctx.raw_hits.clear()
        raise PhaseError("chunk_search", self.cause)


@pytest.mark.asyncio
async def test_operational_level1_failure_raises_search_unavailable() -> None:
    request = SearchRequest(query="test", level=1, top_k=10)
    engine = SearchEngine()
    failure = MilvusException(message="unavailable", code=1)

    with patch.object(engine, "_resolve_l1_pipeline", return_value=FailingLevel1Pipeline(failure)):
        with pytest.raises(SearchUnavailable) as exc_info:
            await engine.search(request)

    assert (exc_info.value.phase_name, exc_info.value.cause) == ("paper_search", failure)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("connection failed"),
        httpx.RemoteProtocolError("peer disconnected"),
        httpx.HTTPStatusError(
            "rate limited",
            request=httpx.Request("POST", "https://embedding.test/embeddings"),
            response=httpx.Response(
                429,
                request=httpx.Request("POST", "https://embedding.test/embeddings"),
            ),
        ),
        httpx.HTTPStatusError(
            "upstream unavailable",
            request=httpx.Request("POST", "https://embedding.test/embeddings"),
            response=httpx.Response(
                503,
                request=httpx.Request("POST", "https://embedding.test/embeddings"),
            ),
        ),
    ],
    ids=["timeout", "network", "protocol", "rate-limit", "server-error"],
)
async def test_transient_embedding_failure_raises_search_unavailable(
    failure: Exception,
) -> None:
    request = SearchRequest(query="test", level=1, top_k=10)
    engine = SearchEngine()

    with patch.object(engine, "_resolve_l1_pipeline", return_value=FailingLevel1Pipeline(failure)):
        with pytest.raises(SearchUnavailable) as exc_info:
            await engine.search(request)

    assert (exc_info.value.phase_name, exc_info.value.cause) == ("paper_search", failure)


@pytest.mark.asyncio
async def test_non_transient_embedding_http_failure_remains_a_phase_error() -> None:
    request = SearchRequest(query="test", level=1, top_k=10)
    engine = SearchEngine()
    http_request = httpx.Request("POST", "https://embedding.test/embeddings")
    failure = httpx.HTTPStatusError(
        "invalid request",
        request=http_request,
        response=httpx.Response(400, request=http_request),
    )

    with (
        patch.object(engine, "_resolve_l1_pipeline", return_value=FailingLevel1Pipeline(failure)),
        pytest.raises(PhaseError) as exc_info,
    ):
        await engine.search(request)

    assert exc_info.value.cause is failure


def test_default_thorough_budget_covers_cold_chunk_search() -> None:
    rpc_timeout = Settings.model_fields["search_level2_rpc_timeout_seconds"].default
    total_timeout = Settings.model_fields["search_level2_timeout_seconds"].default

    assert rpc_timeout >= 30.0
    assert total_timeout >= rpc_timeout + 10.0


@pytest.mark.asyncio
async def test_slow_level2_raises_strict_unavailable_within_budget() -> None:
    request = SearchRequest(query="test", level=2, top_k=10)
    level1_context = _level1_context(request)
    engine = SearchEngine()

    with (
        patch.object(
            engine, "_resolve_l1_pipeline", return_value=StubLevel1Pipeline(level1_context)
        ),
        patch.object(engine, "_resolve_l2_pipeline", return_value=SlowLevel2Pipeline()),
        patch("scholight.search.engine.settings.search_level2_timeout_seconds", 0.02),
    ):
        started = time.perf_counter()
        with pytest.raises(ThoroughSearchUnavailable) as exc_info:
            await engine.search(request)
        elapsed = time.perf_counter() - started

    assert (elapsed < 0.2, exc_info.value.phase_name, level1_context.raw_hits) == (
        True,
        "level2",
        [_paper("A", 0.9, title="Level 1")],
    )


@pytest.mark.asyncio
async def test_operational_level2_failure_raises_strict_unavailable() -> None:
    request = SearchRequest(query="test", level=2, top_k=10)
    level1_context = _level1_context(request)
    engine = SearchEngine()
    failure = MilvusException(message="unavailable", code=1)

    with (
        patch.object(
            engine, "_resolve_l1_pipeline", return_value=StubLevel1Pipeline(level1_context)
        ),
        patch.object(engine, "_resolve_l2_pipeline", return_value=FailingLevel2Pipeline(failure)),
    ):
        with pytest.raises(ThoroughSearchUnavailable) as exc_info:
            await engine.search(request)

    assert (exc_info.value.phase_name, exc_info.value.cause) == ("chunk_search", failure)


@pytest.mark.asyncio
async def test_grpc_level2_failure_raises_strict_unavailable() -> None:
    request = SearchRequest(query="test", level=2, top_k=10)
    level1_context = _level1_context(request)
    engine = SearchEngine()
    failure = grpc.RpcError("deadline exceeded")

    with (
        patch.object(
            engine, "_resolve_l1_pipeline", return_value=StubLevel1Pipeline(level1_context)
        ),
        patch.object(engine, "_resolve_l2_pipeline", return_value=FailingLevel2Pipeline(failure)),
    ):
        with pytest.raises(ThoroughSearchUnavailable) as exc_info:
            await engine.search(request)

    assert (exc_info.value.phase_name, exc_info.value.cause) == ("chunk_search", failure)


@pytest.mark.asyncio
async def test_level2_programming_error_is_not_swallowed() -> None:
    request = SearchRequest(query="test", level=2, top_k=10)
    level1_context = _level1_context(request)
    engine = SearchEngine()

    with (
        patch.object(
            engine, "_resolve_l1_pipeline", return_value=StubLevel1Pipeline(level1_context)
        ),
        patch.object(
            engine,
            "_resolve_l2_pipeline",
            return_value=FailingLevel2Pipeline(ValueError("bad invariant")),
        ),
    ):
        with pytest.raises(PhaseError, match="bad invariant"):
            await engine.search(request)


@pytest.mark.asyncio
async def test_equal_scores_sort_by_arxiv_id_before_limit_and_rank() -> None:
    request = SearchRequest(query="test", level=1, top_k=2)
    context = _level1_context(request)
    context.raw_hits = [
        _paper("C", 1.0),
        _paper("A", 1.0),
        _paper("B", 1.0),
        _paper("Z", 0.5),
    ]
    engine = SearchEngine()

    with (
        patch.object(engine, "_resolve_l1_pipeline", return_value=StubLevel1Pipeline(context)),
        patch(
            "scholight.search.engine._collection_row_counts",
            new_callable=AsyncMock,
            return_value=(None, None),
        ),
    ):
        result = await engine.search(request)

    assert [(hit.rank, hit.arxiv_id, hit.score) for hit in result.hits] == [
        (1, "A", 1.0),
        (2, "B", 1.0),
    ]


@pytest.mark.asyncio
async def test_invalid_rank_candidates_are_skipped_and_valid_candidates_fill_limit() -> None:
    request = SearchRequest(query="test", level=1, top_k=2)
    context = _level1_context(request)
    context.raw_hits = [
        _paper("bad-score", float("nan")),
        _paper("", 10.0),
        _paper("B", 0.9),
        _paper("A", 1.0),
    ]
    engine = SearchEngine()

    with (
        patch.object(engine, "_resolve_l1_pipeline", return_value=StubLevel1Pipeline(context)),
        patch(
            "scholight.search.engine._collection_row_counts",
            new_callable=AsyncMock,
            return_value=(None, None),
        ),
    ):
        result = await engine.search(request)

    assert [(hit.rank, hit.arxiv_id) for hit in result.hits] == [(1, "A"), (2, "B")]


@pytest.mark.asyncio
async def test_missing_optional_candidate_metadata_is_normalized() -> None:
    request = SearchRequest(query="test", level=1, top_k=10)
    context = _level1_context(request)
    candidate = _paper("A", 1.0)
    candidate.update(title="   ", created="", updated="not-a-date", version=0)
    context.raw_hits = [candidate]
    engine = SearchEngine()

    with (
        patch.object(engine, "_resolve_l1_pipeline", return_value=StubLevel1Pipeline(context)),
        patch(
            "scholight.search.engine._collection_row_counts",
            new_callable=AsyncMock,
            return_value=(None, None),
        ),
    ):
        result = await engine.search(request)

    hit = result.hits[0]
    assert (hit.title, hit.created, hit.updated, hit.version) == (
        "arXiv:A",
        None,
        None,
        None,
    )


@pytest.mark.asyncio
async def test_all_invalid_rank_candidates_return_empty_result() -> None:
    request = SearchRequest(query="test", level=1, top_k=10)
    context = _level1_context(request)
    context.raw_hits = [_paper("", 1.0), _paper("A", float("inf"))]
    engine = SearchEngine()

    with (
        patch.object(engine, "_resolve_l1_pipeline", return_value=StubLevel1Pipeline(context)),
        patch(
            "scholight.search.engine._collection_row_counts",
            new_callable=AsyncMock,
            return_value=(None, None),
        ),
    ):
        result = await engine.search(request)

    assert result.hits == []


@pytest.mark.asyncio
async def test_collection_stats_fetch_runs_off_event_loop() -> None:
    event_loop_thread = threading.get_ident()
    worker_threads: list[int] = []

    def fetch_stats() -> tuple[int, int]:
        worker_threads.append(threading.get_ident())
        return 10, 20

    with (
        patch.object(engine_module, "_collection_stats_cache", None),
        patch.object(engine_module, "_fetch_collection_row_counts", side_effect=fetch_stats),
    ):
        row_counts = await engine_module._collection_row_counts()

    assert row_counts == (10, 20)
    assert worker_threads and all(thread_id != event_loop_thread for thread_id in worker_threads)


@pytest.mark.asyncio
async def test_collection_stats_timeout_returns_optional_counts_within_budget() -> None:
    def fetch_stats() -> tuple[int, int]:
        time.sleep(0.1)
        return 10, 20

    with (
        patch.object(engine_module, "_collection_stats_cache", None),
        patch.object(engine_module, "_COLLECTION_STATS_TIMEOUT_SECONDS", 0.01),
        patch.object(engine_module, "_fetch_collection_row_counts", side_effect=fetch_stats),
    ):
        started = time.perf_counter()
        row_counts = await engine_module._collection_row_counts()
        elapsed = time.perf_counter() - started

    assert row_counts == (None, None)
    assert elapsed < 0.05


@pytest.mark.asyncio
async def test_collection_stats_are_cached() -> None:
    fetch_stats = MagicMock(return_value=(10, 20))

    with (
        patch.object(engine_module, "_collection_stats_cache", None),
        patch.object(engine_module, "_fetch_collection_row_counts", fetch_stats),
    ):
        first_counts = await engine_module._collection_row_counts()
        second_counts = await engine_module._collection_row_counts()

    assert (first_counts, second_counts, fetch_stats.call_count) == ((10, 20), (10, 20), 1)


@pytest.mark.asyncio
async def test_collection_stats_concurrent_cache_miss_is_single_flight() -> None:
    calls = 0

    def fetch_stats() -> tuple[int, int]:
        nonlocal calls
        calls += 1
        time.sleep(0.01)
        return 10, 20

    with (
        patch.object(engine_module, "_collection_stats_cache", None),
        patch.object(engine_module, "_collection_stats_lock", None),
        patch.object(engine_module, "_fetch_collection_row_counts", side_effect=fetch_stats),
    ):
        results = await asyncio.gather(*(engine_module._collection_row_counts() for _ in range(10)))

    assert results == [(10, 20)] * 10
    assert calls == 1


def test_collection_stats_rpc_is_explicitly_bounded() -> None:
    client = MagicMock()
    client.get_collection_stats.side_effect = [{"row_count": 10}, {"row_count": 20}]

    with patch("scholight.search.engine.get_client", return_value=client):
        row_counts = engine_module._fetch_collection_row_counts()

    timeouts = [call.kwargs["timeout"] for call in client.get_collection_stats.call_args_list]
    assert (row_counts, timeouts) == (
        (10, 20),
        [
            engine_module._COLLECTION_STATS_TIMEOUT_SECONDS,
            engine_module._COLLECTION_STATS_TIMEOUT_SECONDS,
        ],
    )
