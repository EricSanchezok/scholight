"""Survey Control Lambda serialization contracts."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from scholight.survey import control_lambda


class _Acquire:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    async def __aenter__(self) -> object:
        return self._connection

    async def __aexit__(self, *args: object) -> None:
        return None


class _Pool:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self._connection)


@pytest.mark.asyncio
async def test_control_cycle_exits_cleanly_when_database_lock_is_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = object()
    control = AsyncMock()
    try_lock = AsyncMock(return_value=False)
    unlock = AsyncMock()
    monkeypatch.setattr(control_lambda, "try_lock_survey_control", try_lock)
    monkeypatch.setattr(control_lambda, "unlock_survey_control", unlock)

    result = await control_lambda._run_control_cycle(
        pool=cast(Any, _Pool(connection)),
        control=cast(Any, control),
        event={"source": "duplicate"},
    )

    assert result == {
        "lock_acquired": 0,
        "event_stops": 0,
        "reconciled": 0,
        "launched": 0,
        "cleanups": 0,
        "notifications": 0,
    }
    try_lock.assert_awaited_once_with(connection)
    control.run_cycle.assert_not_awaited()
    unlock.assert_not_awaited()


@pytest.mark.asyncio
async def test_control_cycle_releases_database_lock_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = object()
    cycle_result = {
        "event_stops": 1,
        "reconciled": 2,
        "launched": 3,
        "cleanups": 1,
        "notifications": 1,
    }
    control = AsyncMock()
    control.run_cycle.return_value = cycle_result
    monkeypatch.setattr(
        control_lambda,
        "try_lock_survey_control",
        AsyncMock(return_value=True),
    )
    unlock = AsyncMock()
    monkeypatch.setattr(control_lambda, "unlock_survey_control", unlock)

    result = await control_lambda._run_control_cycle(
        pool=cast(Any, _Pool(connection)),
        control=cast(Any, control),
        event={"source": "timer"},
    )

    assert result == {"lock_acquired": 1, **cycle_result}
    unlock.assert_awaited_once_with(connection)


@pytest.mark.asyncio
async def test_control_cycle_releases_database_lock_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = object()
    control = AsyncMock()
    control.run_cycle.side_effect = RuntimeError("cycle failed")
    monkeypatch.setattr(
        control_lambda,
        "try_lock_survey_control",
        AsyncMock(return_value=True),
    )
    unlock = AsyncMock()
    monkeypatch.setattr(control_lambda, "unlock_survey_control", unlock)

    with pytest.raises(RuntimeError, match="cycle failed"):
        await control_lambda._run_control_cycle(
            pool=cast(Any, _Pool(connection)),
            control=cast(Any, control),
            event={},
        )

    unlock.assert_awaited_once_with(connection)
