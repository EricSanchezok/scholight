"""Bounded paper overlap must preserve lease ownership and shutdown safety."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from scholight.config import settings
from scholight.db.queries_ingestion import IngestionJob
from scholight.scheduler.ingest_worker import (
    IngestLimits,
    drain_ingest,
    process_job,
    run_worker_once,
)
from scholight.scheduler.resources import DownloadedResource
from scholight.scheduler.tests.test_ingest_worker import _job


@pytest.mark.asyncio
async def test_four_lanes_overlap_but_never_exceed_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "pg_pool_max_size", 5)
    ready = asyncio.Event()
    stop = asyncio.Event()
    active = peak = calls = 0
    workers: set[str] = set()

    async def attempt(worker: str, **kwargs: Any) -> bool:
        nonlocal active, peak, calls
        active += 1
        calls += 1
        workers.add(worker)
        peak = max(peak, active)
        assert kwargs["limits"] is not None
        if active == 4:
            ready.set()
        await asyncio.wait_for(ready.wait(), 1)
        stop.set()
        active -= 1
        return True

    with (
        patch("scholight.scheduler.ingest_worker.configured_queue", return_value=object()),
        patch("scholight.scheduler.ingest_worker.run_worker_once", side_effect=attempt),
        patch("scholight.scheduler.ingest_worker.get_ingestion_status", AsyncMock()),
    ):
        result = await drain_ingest(concurrency=4, stop_event=stop)
    assert peak == calls == result.jobs_processed == 4
    assert len(workers) == 4
    assert active == 0
    assert result.reason == "signal"


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [0, 5])
async def test_invalid_concurrency_fails_before_claim(concurrency: int) -> None:
    with patch("scholight.scheduler.ingest_worker.run_worker_once", AsyncMock()) as claim:
        with pytest.raises(ValueError, match="concurrency"):
            await drain_ingest(concurrency=concurrency)
    claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_parallel_requires_connection_for_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "pg_pool_max_size", 4)
    with (
        patch("scholight.scheduler.ingest_worker.configured_queue", return_value=object()),
        patch("scholight.scheduler.ingest_worker.run_worker_once", AsyncMock()) as claim,
    ):
        with pytest.raises(ValueError, match="connection"):
            await drain_ingest(concurrency=4)
    claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_parallel_requires_destination_fencing() -> None:
    with patch("scholight.scheduler.ingest_worker.configured_queue", return_value=None):
        with pytest.raises(ValueError, match="destination"):
            await drain_ingest(concurrency=2)


@pytest.mark.asyncio
async def test_outer_cancellation_joins_processing_and_releases_lease(tmp_path: Path) -> None:
    started = asyncio.Event()
    joined = asyncio.Event()

    async def process(*args: object, **kwargs: object) -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            joined.set()

    with (
        patch("scholight.scheduler.ingest_worker.verified_sync_source", AsyncMock()),
        patch(
            "scholight.scheduler.ingest_worker.claim_ingestion_job", AsyncMock(return_value=_job())
        ),
        patch("scholight.scheduler.ingest_worker.process_job", side_effect=process),
        patch("scholight.scheduler.ingest_worker.release_ingestion_job", AsyncMock()) as release,
        patch("scholight.scheduler.ingest_worker.fail_ingestion_job", AsyncMock()) as fail,
    ):
        task = asyncio.create_task(run_worker_once("worker", scratch_root=tmp_path))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert joined.is_set()
    release.assert_awaited_once_with("2401.00001", "worker")
    fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_parallel_deadline_joins_all_lanes_and_releases_every_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "pg_pool_max_size", 5)
    started: set[str] = set()
    joined: set[str] = set()

    async def claim(worker: str, _lease_seconds: int) -> IngestionJob:
        job = _job(f"2401.{len(started) + 1:05d}")
        started.add(job.arxiv_id)
        return job

    async def process(job: IngestionJob, **kwargs: object) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            joined.add(job.arxiv_id)

    with (
        patch("scholight.scheduler.ingest_worker.configured_queue", return_value=object()),
        patch("scholight.scheduler.ingest_worker.verified_sync_source", AsyncMock()),
        patch("scholight.scheduler.ingest_worker.claim_ingestion_job", side_effect=claim),
        patch("scholight.scheduler.ingest_worker.process_job", side_effect=process),
        patch("scholight.scheduler.ingest_worker.release_ingestion_job", AsyncMock()) as release,
        patch("scholight.scheduler.ingest_worker.fail_ingestion_job", AsyncMock()) as fail,
        patch("scholight.scheduler.ingest_worker.get_ingestion_status", AsyncMock()),
    ):
        result = await drain_ingest(
            concurrency=4,
            stop_event=asyncio.Event(),
            max_runtime_seconds=0.1,
        )
    assert result.reason == "max_runtime"
    assert len(joined) == 4
    assert joined == started == {c.args[0] for c in release.await_args_list}
    fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_and_parser_are_single_lane_but_embedding_overlaps(tmp_path: Path) -> None:
    active = dict.fromkeys(("download", "parser", "embedding"), 0)
    peak = dict(active)
    all_embedding = asyncio.Event()
    limits = IngestLimits(asyncio.Semaphore(1), asyncio.Semaphore(1))

    def enter(stage: str) -> None:
        active[stage] += 1
        peak[stage] = max(peak[stage], active[stage])

    def fetch(_arxiv_id: str, _version: int, scratch: Path) -> DownloadedResource:
        enter("download")
        time.sleep(0.01)
        active["download"] -= 1
        return DownloadedResource("latex", scratch)

    async def parse(*args: object, **kwargs: object) -> tuple[str, str, dict[str, bool]]:
        enter("parser")
        await asyncio.sleep(0.02)
        active["parser"] -= 1
        return "body", "latex", {"has_latex": True}

    class Embedder:
        async def __aenter__(self) -> Embedder:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            enter("embedding")
            if active["embedding"] == 4:
                all_embedding.set()
            await asyncio.wait_for(all_embedding.wait(), 2)
            active["embedding"] -= 1
            return [[0.1, 0.2] for _ in texts]

    with (
        patch("scholight.scheduler.ingest_worker.configured_queue", return_value=None),
        patch("scholight.scheduler.ingest_worker.get_paper", return_value={"version": 1}),
        patch("scholight.scheduler.ingest_worker.fetch_paper_resource", side_effect=fetch),
        patch("scholight.scheduler.ingest_worker._parse_resource", side_effect=parse),
        patch(
            "scholight.scheduler.ingest_worker.chunk_markdown",
            return_value=[
                SimpleNamespace(content="body", chunk_index=0),
            ],
        ),
        patch("scholight.scheduler.ingest_worker.Embedder", Embedder),
        patch("scholight.scheduler.ingest_worker.install_paper_chunks"),
    ):
        outcomes = await asyncio.gather(
            *[
                process_job(_job(f"2401.{i:05d}"), scratch_root=tmp_path, limits=limits)
                for i in range(1, 5)
            ]
        )
    assert outcomes == ["installed"] * 4
    assert peak == {"download": 1, "parser": 1, "embedding": 4}
    assert not any(active.values())
    assert not list(tmp_path.iterdir())
