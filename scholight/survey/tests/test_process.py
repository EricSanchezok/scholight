"""Survey subprocess safety primitives."""

from __future__ import annotations

import asyncio

import pytest

from scholight.survey.process import (
    ProcessControl,
    classify_rcm_error,
    is_transient_rcm_error,
    provider_retry_delay_seconds,
    read_sanitized_tail,
)


@pytest.mark.parametrize(
    ("diagnostics", "error_code"),
    [
        (
            "Error: HTTP 504 Gateway Timeout: upstream request timeout",
            "survey_provider_unavailable",
        ),
        ("request failed with status 503", "survey_provider_unavailable"),
        ("connection reset by peer", "survey_provider_unavailable"),
        ("HTTP 429: upstream throttled", "survey_provider_rate_limited"),
    ],
)
def test_transient_provider_failures_are_classified_for_retry(
    diagnostics: str,
    error_code: str,
) -> None:
    classified, _message = classify_rcm_error(diagnostics)

    assert classified == error_code
    assert is_transient_rcm_error(classified) is True


def test_provider_retry_backoff_is_bounded() -> None:
    assert provider_retry_delay_seconds(1, base=2, maximum=5) == 2
    assert provider_retry_delay_seconds(2, base=2, maximum=5) == 4
    assert provider_retry_delay_seconds(3, base=2, maximum=5) == 5


def test_authentication_failure_is_never_automatically_retried() -> None:
    error_code, _message = classify_rcm_error("HTTP status 401: invalid API key")

    assert error_code == "survey_model_auth_failed"
    assert is_transient_rcm_error(error_code) is False


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
