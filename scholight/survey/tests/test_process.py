"""Survey subprocess safety primitives."""

from __future__ import annotations

import asyncio

import pytest

from scholight.survey.process import ProcessControl, read_sanitized_tail


@pytest.mark.asyncio
async def test_stderr_tail_redacts_credentials_and_host_paths() -> None:
    stream = asyncio.StreamReader()
    stream.feed_data(
        b"Authorization: Bearer secret-token\n"
        b"api_key=provider-secret\n"
        b"failed at /data/surveys/job/run/file.md and /etc/passwd\n"
    )
    stream.feed_eof()

    result = await read_sanitized_tail(stream)

    assert "secret-token" not in result
    assert "provider-secret" not in result
    assert "/data/" not in result
    assert "/etc/" not in result
    assert "<redacted>" in result
    assert "<redacted-path>" in result


@pytest.mark.asyncio
async def test_process_attached_after_lease_loss_is_terminated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated: list[object] = []

    async def _terminate(process: object, *, grace_seconds: float = 10) -> None:
        del grace_seconds
        terminated.append(process)

    monkeypatch.setattr("scholight.survey.process.terminate_process_group", _terminate)
    control = ProcessControl()
    process = object()

    await control.lose_lease()
    await control.attach(process)  # type: ignore[arg-type]

    assert terminated == [process]
