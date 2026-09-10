"""Lean synchronization retains recovery work without scheduling ingestion."""

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock

import pytest

from scholight.config import settings
from scholight.scheduler import metadata_sync
from scholight.store.ingestion import MetadataOutcome


@pytest.mark.asyncio
async def test_batched_sync_releases_vectors_and_commits_each_ledger_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "runtime_profile", "lean")
    monkeypatch.setattr(settings, "metadata_sync_batch_size", 2, raising=False)
    papers = [{"arxiv_id": str(index)} for index in range(5)]
    monkeypatch.setattr(metadata_sync, "_fetch_day", AsyncMock(return_value=(papers, "oai")))
    operations: list[tuple[str, int]] = []

    async def embed(batch: list[dict[str, Any]]) -> None:
        operations.append(("embed", len(batch)))
        for paper in batch:
            paper["abstract_embedding"] = [0.5]

    def write(batch: list[dict[str, Any]]) -> list[MetadataOutcome]:
        operations.append(("write", len(batch)))
        return [MetadataOutcome(paper["arxiv_id"], 1, "new") for paper in batch]

    async def ledger(rows: list[tuple[str, int]], _date: dt.date) -> None:
        operations.append(("ledger", len(rows)))

    monkeypatch.setattr(metadata_sync, "_normalize_and_embed", embed)
    monkeypatch.setattr(metadata_sync, "write_metadata_papers", write)
    monkeypatch.setattr(metadata_sync, "record_deferred_fulltext", ledger)
    result = await metadata_sync._sync_day(dt.date(2026, 9, 10), dt.date(2026, 9, 10))
    assert (result, operations, papers) == (
        (5, "oai"),
        [(operation, size) for size in (2, 2, 1) for operation in ("embed", "write", "ledger")],
        [{"arxiv_id": str(index)} for index in range(5)],
    )


@pytest.mark.asyncio
async def test_second_batch_failure_keeps_daily_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    from scholight.db.queries_ingestion import SyncState

    monkeypatch.setattr(settings, "runtime_profile", "lean")
    monkeypatch.setattr(settings, "metadata_sync_batch_size", 1, raising=False)
    monkeypatch.setattr(metadata_sync, "mark_sync_started", AsyncMock())
    monkeypatch.setattr(metadata_sync, "mark_sync_failed", AsyncMock())
    monkeypatch.setattr(
        metadata_sync,
        "get_sync_state",
        AsyncMock(return_value=SyncState("arxiv", dt.date(2026, 9, 8), None, None)),
    )
    succeeded = AsyncMock()
    monkeypatch.setattr(metadata_sync, "mark_sync_succeeded", succeeded)
    monkeypatch.setattr(metadata_sync, "_fetch_day", AsyncMock(return_value=([{}, {}], "oai")))
    monkeypatch.setattr(metadata_sync, "_normalize_and_embed", AsyncMock())
    monkeypatch.setattr(
        metadata_sync,
        "write_metadata_papers",
        lambda rows: [MetadataOutcome(str(i), 1, "new") for i in range(len(rows))],
    )
    ledger = AsyncMock(side_effect=[None, RuntimeError("ledger unavailable")])
    monkeypatch.setattr(metadata_sync, "record_deferred_fulltext", ledger)
    result = await metadata_sync.run_sync(today=dt.date(2026, 9, 10))
    assert (result["failed_date"], succeeded.await_count) == ("2026-09-09", 0)


@pytest.mark.asyncio
async def test_lean_replay_records_version_without_enqueuing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "runtime_profile", "lean")
    paper = {"arxiv_id": "2401.00001", "version": 2, "_version_available": True}
    monkeypatch.setattr(metadata_sync, "_fetch_day", AsyncMock(return_value=([paper], "oai")))
    monkeypatch.setattr(metadata_sync, "_normalize_and_embed", AsyncMock())
    monkeypatch.setattr(
        metadata_sync, "write_metadata_papers", lambda _: [MetadataOutcome("2401.00001", 2, None)]
    )
    deferred = AsyncMock()
    monkeypatch.setattr(metadata_sync, "record_deferred_fulltext", deferred)
    enqueue = AsyncMock(side_effect=AssertionError("must not enqueue"))
    monkeypatch.setattr(metadata_sync, "enqueue_ingestion_job", enqueue)
    await metadata_sync._sync_day(dt.date(2026, 9, 10), dt.date(2026, 9, 10))
    deferred.assert_awaited_once_with([("2401.00001", 2)], dt.date(2026, 9, 10))


@pytest.mark.asyncio
async def test_deferred_failure_prevents_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "runtime_profile", "lean")
    monkeypatch.setattr(metadata_sync, "_fetch_day", AsyncMock(return_value=([{}], "oai")))
    monkeypatch.setattr(metadata_sync, "_normalize_and_embed", AsyncMock())
    monkeypatch.setattr(
        metadata_sync, "write_metadata_papers", lambda _: [MetadataOutcome("2401.00001", 1, "new")]
    )
    monkeypatch.setattr(
        metadata_sync,
        "record_deferred_fulltext",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        await metadata_sync._sync_day(dt.date(2026, 9, 10), dt.date(2026, 9, 10))
