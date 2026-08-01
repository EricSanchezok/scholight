from __future__ import annotations

from scholight.models.web_extract import ExtractResponseFormat, RenderMode
from scholight.web_extract.browser import _cookie_map, _split_headers
from scholight.web_extract.engine import ExtractInput


def _request(**overrides: object) -> ExtractInput:
    values: dict[str, object] = {
        "url": "https://example.com",
        "render": RenderMode.ALWAYS,
        "output": ExtractResponseFormat.MAIN_MARKDOWN,
    }
    values.update(overrides)
    return ExtractInput(**values)  # type: ignore[arg-type]


def test_browser_separates_sensitive_headers_for_origin_scoping() -> None:
    regular, sensitive = _split_headers(
        {"Accept-Language": "zh-CN", "Authorization": "Bearer target"}
    )

    assert regular == {"Accept-Language": "zh-CN"}
    assert sensitive == {"Authorization": "Bearer target"}


def test_browser_accepts_cookie_header_as_stateless_cookie_input() -> None:
    request = _request(headers={"Cookie": "session=abc; theme=dark"})

    assert _cookie_map(request) == {"session": "abc", "theme": "dark"}
