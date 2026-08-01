"""Survey artifact cleanup lease contracts."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from scholight.config import settings
from scholight.survey.cleanup_worker import _heartbeat


@pytest.mark.asyncio
async def test_cleanup_heartbeat_database_failure_loses_expired_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_lost = asyncio.Event()
    monkeypatch.setattr(settings, "survey_heartbeat_seconds", 0.001)
    monkeypatch.setattr(settings, "survey_lease_seconds", 0)

    with patch(
        "scholight.survey.cleanup_worker.heartbeat_artifact_cleanup",
        new_callable=AsyncMock,
        side_effect=OSError("database unavailable"),
    ) as heartbeat:
        await asyncio.wait_for(
            _heartbeat(
                uuid4(),
                uuid4(),
                asyncio.Event(),
                lease_lost,
            ),
            timeout=1,
        )

    heartbeat.assert_awaited_once()
    assert lease_lost.is_set()
