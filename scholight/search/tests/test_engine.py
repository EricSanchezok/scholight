"""Unit tests for SearchEngine Level 2 fallback behavior."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pymilvus.exceptions import MilvusException

from scholight.models.search import SearchRequest
from scholight.search import engine as engine_module
from scholight.search.base import PhaseError, PipelineContext
from scholight.search.engine import SearchEngine


def _level1_context(request: SearchRequest) -> PipelineContext:
    return PipelineContext(
        request=request,
        query_vector=[0.0] * 1024,
        raw_hits=[
            {
                "arxiv_id": "A",
                "score": 0.9,
                "title": "Level 1",
                "authors": ["Author"],
            }
        ],
        metadata={"mode": "dense"},
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
async def test_slow_level2_returns_unchanged_level1_hits_within_budget() -> None:
    request = SearchRequest(query="test", level=2, top_k=10)
    level1_context = _level1_context(request)
    engine = SearchEngine()

    with (
        patch.object(
            engine, "_resolve_l1_pipeline", return_value=StubLevel1Pipeline(level1_context)
        ),
        patch.object(engine, "_resolve_l2_pipeline", return_value=SlowLevel2Pipeline()),
        patch("scholight.search.engine.settings.search_level2_timeout_seconds", 0.02),
        patch(
            "scholight.search.engine._collection_row_counts",
            new_callable=AsyncMock,
            return_value=(None, None),
        ),
    ):
        started = time.perf_counter()
        search_result = await engine.search(request)
        elapsed = time.perf_counter() - started

    assert elapsed < 0.2
    assert [(hit.arxiv_id, hit.score) for hit in search_result.hits] == [("A", 0.9)]
    assert level1_context.raw_hits == [
        {
            "arxiv_id": "A",
            "score": 0.9,
            "title": "Level 1",
            "authors": ["Author"],
        }
    ]


@pytest.mark.asyncio
async def test_operational_level2_failure_returns_unchanged_level1_hits() -> None:
    request = SearchRequest(query="test", level=2, top_k=10)
    level1_context = _level1_context(request)
    engine = SearchEngine()
    failure = MilvusException(message="unavailable", code=1)

    with (
        patch.object(
            engine, "_resolve_l1_pipeline", return_value=StubLevel1Pipeline(level1_context)
        ),
        patch.object(engine, "_resolve_l2_pipeline", return_value=FailingLevel2Pipeline(failure)),
        patch(
            "scholight.search.engine._collection_row_counts",
            new_callable=AsyncMock,
            return_value=(None, None),
        ),
    ):
        search_result = await engine.search(request)

    assert [(hit.arxiv_id, hit.score) for hit in search_result.hits] == [("A", 0.9)]


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
