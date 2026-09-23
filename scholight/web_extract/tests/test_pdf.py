from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import patch

import pymupdf
import pytest

from scholight.web_extract.engine import _pdf
from scholight.web_extract.errors import ExtractError


def test_extract_extra_declares_its_pdf_runtime_dependencies() -> None:
    project = Path(__file__).parents[3] / "pyproject.toml"
    dependencies = tomllib.loads(project.read_text())["project"]["optional-dependencies"]["extract"]

    assert {item.split(">=")[0] for item in dependencies} >= {"pymupdf", "pymupdf4llm"}


def test_pdf_extracts_real_document_content_and_metadata() -> None:
    with pymupdf.open() as document:
        document.set_metadata({"title": "Extract fixture", "author": "Scholight"})
        document.new_page().insert_text((72, 72), "A real PDF extraction fixture.")
        data = document.tobytes()

    result = _pdf(data)

    assert ("A real PDF extraction fixture." in result.content, result.title, result.author) == (
        True,
        "Extract fixture",
        "Scholight",
    )


@pytest.mark.parametrize("fail", [False, True])
def test_pdf_closes_the_document_even_when_conversion_fails(fail: bool) -> None:
    document = pymupdf.open()
    conversion = {"side_effect": ValueError("invalid page")} if fail else {"return_value": "Body"}
    with (
        patch("pymupdf.open", return_value=document),
        patch("pymupdf4llm.to_markdown", **conversion),
    ):
        if fail:
            with pytest.raises(ExtractError, match="PDF content could not be extracted"):
                _pdf(b"fixture")
        else:
            _pdf(b"fixture")

    assert document.is_closed


def test_malformed_pdf_returns_the_stable_document_error() -> None:
    with pytest.raises(ExtractError) as error:
        _pdf(b"%PDF-not-a-valid-document")

    assert (error.value.code, error.value.status_code, error.value.retryable) == (
        "extraction_failed",
        422,
        False,
    )
