"""Continuous metadata cursor behavior."""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, patch

import pytest

from scholight.db.queries_ingestion import SyncState
from scholight.scheduler.metadata_sync import _normalize_and_embed, _sync_day, run_sync
from scholight.store.ingestion import MetadataOutcome


@pytest.mark.asyncio
async def test_failed_day_does_not_advance_or_skip_cursor() -> None:
    state = SyncState("arxiv", dt.date(2026, 7, 20), None, None)
    sync_day = AsyncMock(side_effect=[(10, "oai"), RuntimeError("source failed")])
    mark_succeeded = AsyncMock()

    with (
        patch("scholight.scheduler.metadata_sync.mark_sync_started", AsyncMock()),
        patch("scholight.scheduler.metadata_sync.get_sync_state", AsyncMock(return_value=state)),
        patch("scholight.scheduler.metadata_sync._sync_day", sync_day),
        patch(
            "scholight.scheduler.metadata_sync.mark_sync_succeeded",
            mark_succeeded,
        ),
        patch("scholight.scheduler.metadata_sync.mark_sync_failed", AsyncMock()),
    ):
        result = await run_sync(today=dt.date(2026, 7, 23))

    assert result["failed_date"] == "2026-07-22"
    mark_succeeded.assert_awaited_once_with("arxiv", dt.date(2026, 7, 21))


@pytest.mark.asyncio
async def test_up_to_date_sync_only_reconciles_recent_window() -> None:
    state = SyncState("arxiv", dt.date(2026, 7, 22), None, None)
    reconcile = AsyncMock(return_value=3)

    with (
        patch("scholight.scheduler.metadata_sync.mark_sync_started", AsyncMock()),
        patch("scholight.scheduler.metadata_sync.get_sync_state", AsyncMock(return_value=state)),
        patch("scholight.scheduler.metadata_sync._sync_day", AsyncMock()) as sync_day,
        patch("scholight.scheduler.metadata_sync._reconcile_recent", reconcile),
    ):
        result = await run_sync(today=dt.date(2026, 7, 23))

    assert result["reconciled"] == 3
    sync_day.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_trusted_revision_reenqueues_after_cross_store_interruption() -> None:
    paper = {
        "arxiv_id": "2401.00001",
        "version": 2,
        "_version_available": True,
    }
    enqueue = AsyncMock()

    with (
        patch(
            "scholight.scheduler.metadata_sync._fetch_day",
            AsyncMock(return_value=([paper], "oai")),
        ),
        patch("scholight.scheduler.metadata_sync._normalize_and_embed", AsyncMock()),
        patch(
            "scholight.scheduler.metadata_sync.write_metadata_papers",
            return_value=[MetadataOutcome("2401.00001", 2, None)],
        ),
        patch("scholight.scheduler.metadata_sync.enqueue_ingestion_job", enqueue),
    ):
        await _sync_day(dt.date(2026, 7, 23), dt.date(2026, 7, 23))

    enqueue.assert_awaited_once_with("2401.00001", 2, "revision", max_attempts=8)


@pytest.mark.asyncio
async def test_api_metadata_marks_generated_abstract_embedding_as_available() -> None:
    class FakeEmbedder:
        async def __aenter__(self) -> FakeEmbedder:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            assert texts == ["Abstract"]
            return [[0.1, 0.2]]

    paper = {
        "arxiv_id": "2401.00001",
        "abstract": "Abstract",
        "_metadata_fields": {"abstract"},
    }

    with patch("scholight.scheduler.metadata_sync.Embedder", FakeEmbedder):
        await _normalize_and_embed([paper])

    assert paper["_metadata_fields"] == {"abstract", "abstract_embedding"}
