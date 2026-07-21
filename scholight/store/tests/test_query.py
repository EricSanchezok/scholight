"""Focused compatibility tests for store query helpers."""

from unittest.mock import MagicMock, patch

from scholight.store.client import QUERY_CONSISTENCY
from scholight.store.query import _build_filter, batch_get_arxiv_papers, bm25_search_all_chunks


def test_bm25_search_all_chunks_keeps_legacy_timeout() -> None:
    client = MagicMock()
    client.search.return_value = [[]]

    with patch("scholight.store.query.get_client", return_value=client):
        bm25_search_all_chunks("test")

    assert client.search.call_args.kwargs["timeout"] == 120


def test_arxiv_id_filter_escapes_backslashes_before_double_quotes() -> None:
    expression = _build_filter(arxiv_ids=['A\\B"C'])

    assert expression == r'(arxiv_id in ["A\\B\"C"])'


def test_batch_get_arxiv_papers_uses_one_bounded_query_for_public_fields() -> None:
    client = MagicMock()
    client.query.return_value = [
        {"arxiv_id": "B", "abstract": "Abstract B"},
        {"arxiv_id": "A", "abstract": "Abstract A"},
    ]

    with patch("scholight.store.query.get_client", return_value=client):
        papers = batch_get_arxiv_papers(
            ["A", "B"],
            output_fields=["arxiv_id", "abstract"],
            timeout=1.5,
        )

    assert papers == {
        "A": {"arxiv_id": "A", "abstract": "Abstract A"},
        "B": {"arxiv_id": "B", "abstract": "Abstract B"},
    }
    client.query.assert_called_once_with(
        collection_name="arxiv_papers",
        filter='(arxiv_id in ["A", "B"])',
        output_fields=["arxiv_id", "abstract"],
        consistency_level=QUERY_CONSISTENCY,
        timeout=1.5,
    )
