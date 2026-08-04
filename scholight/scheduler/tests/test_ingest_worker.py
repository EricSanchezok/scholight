"""Unified worker invariants."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from scholight.db.queries_ingestion import IngestionJob
from scholight.pipeline.latex_md import LatexMdError, LatexResourceLimitError
from scholight.scheduler.ingest_worker import (
    IngestionShutdownRequestedError,
    InvalidIngestionJobError,
    drain_ingest,
    process_job,
    run_worker_once,
)
from scholight.scheduler.resources import DownloadedResource


def _job(arxiv_id: str = "2401.00001", version: int = 1) -> IngestionJob:
    return IngestionJob(
        arxiv_id=arxiv_id,
        target_version=version,
        source="new",
        priority=10,
        status="running",
        attempt_count=1,
        max_attempts=8,
        lease_owner="worker",
    )


@pytest.mark.asyncio
async def test_newer_metadata_makes_old_job_obsolete_without_download(tmp_path: Path) -> None:
    with (
        patch(
            "scholight.scheduler.ingest_worker.get_paper",
            return_value={"arxiv_id": "2401.00001", "version": 2},
        ),
        patch("scholight.scheduler.ingest_worker.fetch_paper_resource") as fetch,
    ):
        outcome = await process_job(_job(), scratch_root=tmp_path)

    assert outcome == "obsolete"
    fetch.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_id_is_dead_before_scratch_or_network(tmp_path: Path) -> None:
    with pytest.raises(InvalidIngestionJobError):
        await process_job(_job("not-an-id"), scratch_root=tmp_path)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_latex_parse_failure_falls_back_to_exact_pdf(tmp_path: Path) -> None:
    latex_dir = tmp_path / "latex"
    pdf_path = tmp_path / "paper.pdf"
    chunks = [SimpleNamespace(content="usable content", chunk_index=0)]

    class FakeEmbedder:
        async def __aenter__(self) -> FakeEmbedder:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def embed_many(self, values: list[str]) -> list[list[float]]:
            return [[0.1, 0.2] for _ in values]

    with (
        patch(
            "scholight.scheduler.ingest_worker.get_paper",
            return_value={"arxiv_id": "2401.00001", "version": 1},
        ),
        patch(
            "scholight.scheduler.ingest_worker.fetch_paper_resource",
            return_value=DownloadedResource("latex", latex_dir),
        ),
        patch(
            "scholight.scheduler.ingest_worker.latex_to_markdown",
            side_effect=LatexMdError("pandoc failed"),
        ),
        patch(
            "scholight.scheduler.ingest_worker.fetch_pdf_resource",
            return_value=DownloadedResource("pdf", pdf_path),
        ) as fetch_pdf,
        patch(
            "scholight.scheduler.ingest_worker.pdf_to_markdown",
            return_value="# Recovered from PDF\n\nBody",
        ),
        patch(
            "scholight.scheduler.ingest_worker.chunk_markdown",
            return_value=chunks,
        ) as chunk,
        patch("scholight.scheduler.ingest_worker.Embedder", FakeEmbedder),
        patch("scholight.scheduler.ingest_worker.install_paper_chunks") as install,
    ):
        outcome = await process_job(_job(), scratch_root=tmp_path)

    assert outcome == "installed"
    fetch_pdf.assert_called_once()
    chunk.assert_called_once_with("# Recovered from PDF\n\nBody", source="pdf")
    assert install.call_args.kwargs["resource_flags"] == {
        "has_pdf": True,
        "has_latex": False,
        "has_markdown": True,
    }


@pytest.mark.asyncio
async def test_latex_resource_limit_falls_back_without_job_retry(tmp_path: Path) -> None:
    latex_dir = tmp_path / "latex"
    pdf_path = tmp_path / "paper.pdf"
    chunks = [SimpleNamespace(content="usable content", chunk_index=0)]

    class FakeEmbedder:
        async def __aenter__(self) -> FakeEmbedder:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def embed_many(self, values: list[str]) -> list[list[float]]:
            return [[0.1, 0.2] for _ in values]

    with (
        patch(
            "scholight.scheduler.ingest_worker.get_paper",
            return_value={"arxiv_id": "2401.00001", "version": 1},
        ),
        patch(
            "scholight.scheduler.ingest_worker.fetch_paper_resource",
            return_value=DownloadedResource("latex", latex_dir),
        ),
        patch(
            "scholight.scheduler.ingest_worker.latex_to_markdown",
            side_effect=LatexResourceLimitError("pandoc resource limit"),
        ),
        patch(
            "scholight.scheduler.ingest_worker.fetch_pdf_resource",
            return_value=DownloadedResource("pdf", pdf_path),
        ),
        patch(
            "scholight.scheduler.ingest_worker.pdf_to_markdown",
            return_value="# Recovered from PDF\n\nBody",
        ),
        patch("scholight.scheduler.ingest_worker.chunk_markdown", return_value=chunks),
        patch("scholight.scheduler.ingest_worker.Embedder", FakeEmbedder),
        patch("scholight.scheduler.ingest_worker.install_paper_chunks"),
    ):
        outcome = await process_job(_job(), scratch_root=tmp_path)

    assert outcome == "installed"


@pytest.mark.asyncio
async def test_empty_latex_markdown_falls_back_to_exact_pdf(tmp_path: Path) -> None:
    with (
        patch(
            "scholight.scheduler.ingest_worker.get_paper",
            return_value={"arxiv_id": "2401.00001", "version": 1},
        ),
        patch(
            "scholight.scheduler.ingest_worker.fetch_paper_resource",
            return_value=DownloadedResource("latex", tmp_path / "latex"),
        ),
        patch("scholight.scheduler.ingest_worker.latex_to_markdown", return_value="  "),
        patch(
            "scholight.scheduler.ingest_worker.fetch_pdf_resource",
            side_effect=LatexMdError("stop after proving fallback"),
        ) as fetch_pdf,
    ):
        with pytest.raises(LatexMdError, match="proving fallback"):
            await process_job(_job(), scratch_root=tmp_path)

    fetch_pdf.assert_called_once()


@pytest.mark.asyncio
async def test_worker_renews_lease_while_processing(tmp_path: Path) -> None:
    async def slow_process(*_args: object, **_kwargs: object) -> str:
        await asyncio.sleep(0.03)
        return "installed"

    renew = AsyncMock(return_value=True)
    with (
        patch(
            "scholight.scheduler.ingest_worker.claim_ingestion_job", AsyncMock(return_value=_job())
        ),
        patch("scholight.scheduler.ingest_worker.process_job", side_effect=slow_process),
        patch("scholight.scheduler.ingest_worker.renew_ingestion_job_lease", renew),
        patch("scholight.scheduler.ingest_worker.complete_ingestion_job", AsyncMock()),
    ):
        worked = await run_worker_once(
            "worker",
            scratch_root=tmp_path,
            heartbeat_interval_seconds=0.005,
        )

    assert worked is True
    assert renew.await_count >= 1


@pytest.mark.asyncio
async def test_worker_releases_claim_when_shutdown_reaches_a_safe_boundary(tmp_path: Path) -> None:
    stop = asyncio.Event()

    async def interrupted(*_args: object, **_kwargs: object) -> str:
        stop.set()
        raise IngestionShutdownRequestedError

    release = AsyncMock(return_value=True)
    complete = AsyncMock()
    with (
        patch(
            "scholight.scheduler.ingest_worker.claim_ingestion_job", AsyncMock(return_value=_job())
        ),
        patch("scholight.scheduler.ingest_worker.process_job", side_effect=interrupted),
        patch("scholight.scheduler.ingest_worker.release_ingestion_job", release),
        patch("scholight.scheduler.ingest_worker.complete_ingestion_job", complete),
    ):
        worked = await run_worker_once("worker", scratch_root=tmp_path, stop_event=stop)

    assert worked is True
    release.assert_awaited_once_with("2401.00001", "worker")
    complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_drain_exits_after_queue_stays_idle() -> None:
    with (
        patch("scholight.scheduler.ingest_worker.run_worker_once", AsyncMock(return_value=False)),
        patch(
            "scholight.scheduler.ingest_worker.get_ingestion_status",
            AsyncMock(
                return_value={"queue": {"backlog": 4, "dead": 1, "oldest_age_seconds": 7200}}
            ),
        ),
        patch("scholight.scheduler.ingest_worker.emit_emf") as emit,
    ):
        result = await drain_ingest(
            idle_grace_seconds=0.01,
            max_runtime_seconds=1,
            poll_seconds=0.002,
            stop_event=asyncio.Event(),
        )

    assert result.reason == "idle"
    assert result.jobs_processed == 0
    assert any(
        call.kwargs
        == {
            "service": "paper-ingest",
            "metrics": {
                "IngestionBacklog": (4, "Count"),
                "IngestionDeadTotal": (1, "Count"),
                "IngestionOldestAge": (7200, "Seconds"),
            },
        }
        for call in emit.call_args_list
    )


@pytest.mark.asyncio
async def test_drain_stops_at_its_runtime_deadline() -> None:
    async def slow_attempt(*_args: object, **_kwargs: object) -> bool:
        await asyncio.sleep(0.01)
        return True

    with patch("scholight.scheduler.ingest_worker.run_worker_once", side_effect=slow_attempt):
        result = await drain_ingest(
            idle_grace_seconds=1,
            max_runtime_seconds=0.025,
            poll_seconds=0.001,
            stop_event=asyncio.Event(),
        )

    assert result.reason == "max_runtime"
    assert result.jobs_processed >= 1
