from __future__ import annotations

import pytest
from pydantic import ValidationError

from scholight.models.web_extract import ExtractRequest, ExtractResponseFormat, RenderMode


def test_extract_request_defaults_to_auto_main_markdown() -> None:
    request = ExtractRequest(url="https://example.com:8443/article")

    assert (request.render, request.output) == (
        RenderMode.AUTO,
        ExtractResponseFormat.MAIN_MARKDOWN,
    )


def test_extract_request_allows_target_authorization_header() -> None:
    request = ExtractRequest(
        url="https://example.com/private",
        headers={"Authorization": "Bearer target-secret"},
    )

    assert request.headers["Authorization"] == "Bearer target-secret"


def test_extract_request_rejects_transport_owned_header() -> None:
    with pytest.raises(ValidationError, match="Host"):
        ExtractRequest(url="https://example.com", headers={"Host": "internal.example"})


def test_extract_request_rejects_cookie_header_and_cookie_map() -> None:
    with pytest.raises(ValidationError, match="Cookie"):
        ExtractRequest(
            url="https://example.com",
            headers={"Cookie": "session=raw"},
            cookies={"session": "mapped"},
        )


def test_extract_request_cursor_is_exclusive() -> None:
    with pytest.raises(ValidationError, match="cursor"):
        ExtractRequest(url="https://example.com", cursor="opaque")


def test_extract_request_accepts_cursor_only() -> None:
    request = ExtractRequest(cursor="opaque")

    assert (request.url, request.cursor) == (None, "opaque")
