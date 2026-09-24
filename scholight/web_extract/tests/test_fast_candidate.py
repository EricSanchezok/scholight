from __future__ import annotations

from unittest.mock import patch

import pytest
import trafilatura

from scholight.models.web_extract import ExtractResponseFormat, RenderMode
from scholight.web_extract.engine import ExtractInput, parse_document
from scholight.web_extract.tests.test_parse_reuse import _fetched
from scholight.web_extract.worker_contracts import WorkerJob


@pytest.mark.parametrize("candidate", [False, True])
def test_fast_candidate_is_explicit_and_default_keeps_full_extraction(candidate) -> None:
    with patch("trafilatura.extract", wraps=trafilatura.extract) as extract:
        result = parse_document(
            _fetched(),
            ExtractInput(
                "https://example.com", RenderMode.AUTO, ExtractResponseFormat.MAIN_MARKDOWN
            ),
            rendered=False,
            fast_html=candidate,
        )
    assert result.extracted is not None
    assert extract.call_count == 1 and extract.call_args.kwargs["fast"] is candidate


def test_private_worker_contract_defaults_to_full_quality() -> None:
    job = WorkerJob(
        request={"url": "https://example.com"},
        result_path="fixture-result.json",
        result_limit=1000,
    )
    assert job.fast_html is False
