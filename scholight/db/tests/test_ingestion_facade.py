"""The destination adapter preserves legacy consumers without inheriting their jobs."""

from unittest.mock import AsyncMock

import pytest

from scholight.config import settings
from scholight.db import queries_ingestion as legacy
from scholight.db.target_ingestion import TargetQueue


@pytest.mark.asyncio
async def test_target_enqueue_never_calls_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    from scholight.db import ingestion

    monkeypatch.setattr(settings, "ingestion_target_id", "a" * 64)
    target = AsyncMock(return_value=True)
    old = AsyncMock(side_effect=AssertionError("legacy queue touched"))
    monkeypatch.setattr(TargetQueue, "enqueue", target)
    monkeypatch.setattr(legacy, "enqueue_ingestion_job", old)
    assert await ingestion.enqueue_ingestion_job("2608.00001", 1, "new", max_attempts=8)
    target.assert_awaited_once_with("2608.00001", 1, "new", max_attempts=8)


@pytest.mark.asyncio
async def test_legacy_queue_remains_available_for_n_minus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scholight.db import ingestion

    monkeypatch.setattr(settings, "ingestion_target_id", "")
    old = AsyncMock(return_value=True)
    monkeypatch.setattr(legacy, "enqueue_ingestion_job", old)
    assert await ingestion.enqueue_ingestion_job("2608.00001", 1, "new", max_attempts=8)
    old.assert_awaited_once()


@pytest.mark.asyncio
async def test_wrong_collection_binding_prevents_cursor_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scholight.db import ingestion
    from scholight.db.client import DBError
    from scholight.models.ingestion_target import IngestionTarget

    monkeypatch.setattr(settings, "ingestion_target_id", "a" * 64)
    monkeypatch.setattr(
        ingestion,
        "_actual_target",
        lambda: IngestionTarget("https://test.invalid", "2", "3", "qwen", 4),
    )
    baseline = AsyncMock(side_effect=AssertionError("baseline accessed on wrong target"))
    monkeypatch.setattr(TargetQueue, "sync_source", baseline)
    with pytest.raises(DBError, match="identity"):
        await ingestion.verified_sync_source()
