"""Lean synchronization retains recovery work without scheduling ingestion."""

import datetime as dt
from unittest.mock import AsyncMock

import pytest

from scholight.config import settings
from scholight.scheduler import metadata_sync
from scholight.store.ingestion import MetadataOutcome


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
