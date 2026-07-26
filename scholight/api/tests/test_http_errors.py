"""Public Scholight HTTP error contract tests."""

from __future__ import annotations

from typing import cast

from scholight.api.http_errors import http_error


def test_http_error_builds_explicit_retry_contract() -> None:
    error = http_error(
        503,
        code="service_unavailable",
        message="The service is temporarily unavailable.",
        retryable=True,
        retry_after=5,
    )

    detail = cast(dict[str, object], error.detail)
    assert detail == {
        "code": "service_unavailable",
        "message": "The service is temporarily unavailable.",
        "retryable": True,
    }
    assert error.headers == {"Retry-After": "5"}


def test_http_error_keeps_non_retryable_errors_header_free() -> None:
    error = http_error(
        404,
        code="resource_not_found",
        message="The resource no longer exists.",
        retryable=False,
        retry_after=None,
    )

    detail = cast(dict[str, object], error.detail)
    assert detail["retryable"] is False
    assert error.headers is None
