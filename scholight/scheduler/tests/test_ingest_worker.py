"""Unified worker invariants."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from scholight.db.queries_ingestion import IngestionJob
from scholight.scheduler.ingest_worker import InvalidIngestionJobError, process_job


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
