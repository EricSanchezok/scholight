"""Passive observability for blocking search SDK dispatch."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from scholight.search.executor import run_search_blocking


@pytest.mark.asyncio
async def test_default_executor_wait_is_measured_without_a_private_worker_limit() -> None:
    with patch("scholight.search.executor.emit_emf") as emit:
        result = await run_search_blocking(lambda value: value + 1, 4)

    assert result == 5
    emit.assert_called_once()
    metrics = emit.call_args.kwargs["metrics"]
    assert set(metrics) == {"ThreadPoolWait"}
    assert metrics["ThreadPoolWait"][0] >= 0
