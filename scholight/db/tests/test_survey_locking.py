"""Survey advisory-lock contracts."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from scholight.db import survey_locking


@pytest.mark.asyncio
async def test_try_lock_survey_control_uses_nonblocking_session_lock() -> None:
    connection = AsyncMock()
    connection.fetchval.return_value = True

    assert await survey_locking.try_lock_survey_control(connection) is True

    query, namespace, key = connection.fetchval.await_args.args
    assert query == "SELECT pg_try_advisory_lock($1, $2)"
    assert isinstance(namespace, int)
    assert key == 0


@pytest.mark.asyncio
async def test_unlock_survey_control_releases_the_same_session_lock() -> None:
    connection = AsyncMock()
    connection.fetchval.return_value = True

    assert await survey_locking.unlock_survey_control(connection) is True

    query, namespace, key = connection.fetchval.await_args.args
    assert query == "SELECT pg_advisory_unlock($1, $2)"
    assert isinstance(namespace, int)
    assert key == 0
