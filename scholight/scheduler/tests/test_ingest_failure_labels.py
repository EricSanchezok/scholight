"""Failure labels are bounded metadata, never provider messages or paper data."""

import httpx
import pytest
from pymilvus.exceptions import MilvusException

from scholight.scheduler import ingest_worker as worker


def test_http_failure_labels_only_include_category_and_numeric_status() -> None:
    request = httpx.Request(
        "POST", "https://test.invalid/private?key=do-not-log", headers={"Authorization": "secret"}
    )
    response = httpx.Response(402, request=request, json={"error": "private response"})
    error = httpx.HTTPStatusError("private message", request=request, response=response)

    assert worker._safe_failure_fields(error) == {
        "error_category": "http_status",
        "http_status": 402,
    }


def test_unknown_failure_label_never_formats_the_exception() -> None:
    class SensitiveError(Exception):
        def __str__(self) -> str:
            raise AssertionError("exception message must not be inspected")

    assert worker._safe_failure_fields(SensitiveError("private data")) == {
        "error_category": "other"
    }


@pytest.mark.parametrize(("code", "expected"), [(1100, {"vector_status": 1100}), (999999, {})])
def test_vector_status_is_bounded_without_logging_provider_message(
    code: int, expected: dict[str, int]
) -> None:
    error = MilvusException(code=code, message="private provider response and paper data")
    assert worker._safe_failure_fields(error) == {"error_category": "vector_error", **expected}
