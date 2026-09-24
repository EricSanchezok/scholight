from __future__ import annotations

from unittest.mock import patch

import pytest

from scholight.models.web_extract import ExtractResponseFormat, RenderMode
from scholight.web_extract.engine import ExtractInput, FetchResult, parse_document
from scholight.web_extract.extractors import extract_html

HTML = (
    "<html><head><title>Evidence</title></head><body><article><p>"
    + ("The original complete paragraph contains all the required evidence. " * 12)
    + "</p></article></body></html>"
)


def _fetched() -> FetchResult:
    return FetchResult(
        requested_url="https://example.com",
        final_url="https://example.com",
        status_code=200,
        content_type="text/html",
        charset="utf-8",
        body=HTML.encode(),
    )


def test_auto_reuses_complete_content_and_metadata_from_quality_parse() -> None:
    expected = extract_html(
        HTML, source_url="https://example.com", output=ExtractResponseFormat.MAIN_MARKDOWN
    )
    with patch("scholight.web_extract.engine.extract_html", wraps=extract_html) as parse:
        result = parse_document(
            _fetched(),
            ExtractInput(
                "https://example.com", RenderMode.AUTO, ExtractResponseFormat.MAIN_MARKDOWN
            ),
            rendered=False,
        )
    assert result.extracted == expected
    assert parse.call_count == 1


@pytest.mark.parametrize(
    "output",
    [
        ExtractResponseFormat.TEXT,
        ExtractResponseFormat.FULL_MARKDOWN,
        ExtractResponseFormat.RAW_HTML,
    ],
)
def test_changed_output_runs_its_own_parse(output: ExtractResponseFormat) -> None:
    with patch("scholight.web_extract.engine.extract_html", wraps=extract_html) as parse:
        result = parse_document(
            _fetched(),
            ExtractInput("https://example.com", RenderMode.AUTO, output),
            rendered=False,
        )
    assert result.extracted == extract_html(HTML, source_url="https://example.com", output=output)
    assert parse.call_count == 2


def test_parse_reuse_can_be_disabled_for_ablation() -> None:
    with patch("scholight.web_extract.engine.extract_html", wraps=extract_html) as parse:
        parse_document(
            _fetched(),
            ExtractInput(
                "https://example.com", RenderMode.AUTO, ExtractResponseFormat.MAIN_MARKDOWN
            ),
            rendered=False,
            reuse_quality=False,
        )
    assert parse.call_count == 2
