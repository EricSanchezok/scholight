"""Continuous metadata cursor behavior."""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, patch

import pytest

from scholight.db.queries_ingestion import SyncState
from scholight.scheduler.metadata_sync import run_sync


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
