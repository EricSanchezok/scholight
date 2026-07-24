"""Unified worker invariants."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scholight.db.queries_ingestion import IngestionJob
from scholight.pipeline.latex_md import LatexMdError
from scholight.scheduler.ingest_worker import InvalidIngestionJobError, process_job
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
