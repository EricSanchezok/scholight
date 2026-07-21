"""Focused compatibility tests for store query helpers."""

from unittest.mock import MagicMock, patch

from scholight.store.query import bm25_search_all_chunks


def test_bm25_search_all_chunks_keeps_legacy_timeout() -> None:
    client = MagicMock()
    client.search.return_value = [[]]

    with patch("scholight.store.query.get_client", return_value=client):
        bm25_search_all_chunks("test")

    assert client.search.call_args.kwargs["timeout"] == 120
