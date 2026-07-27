from __future__ import annotations

from typing import Any

import pytest

from scripts.backfill_authors import backfill_ids, collect_missing_author_ids


class _Iterator:
    def __init__(self, pages: list[list[dict[str, str]]]) -> None:
        self.pages = iter(pages)
        self.closed = False

    def next(self) -> list[dict[str, str]]:
        return next(self.pages, [])

    def close(self) -> None:
        self.closed = True


class _Client:
    def __init__(self) -> None:
        self.iterator = _Iterator([[{"arxiv_id": "2601.00001"}, {"arxiv_id": "2601.00002"}], []])
        self.kwargs: dict[str, Any] = {}

    def query_iterator(self, collection: str, **kwargs: Any) -> _Iterator:
        self.kwargs = {"collection": collection, **kwargs}
        return self.iterator


def test_collect_missing_author_ids_uses_a_bounded_empty_author_scan() -> None:
    client = _Client()

    ids = collect_missing_author_ids(client, limit=2, after="2601.00000")

    assert ids == ["2601.00001", "2601.00002"]
    assert client.kwargs == {
        "collection": "arxiv_papers",
        "batch_size": 1000,
        "limit": 2,
        "filter": "array_length(authors) == 0 and arxiv_id > '2601.00000'",
        "output_fields": ["arxiv_id"],
        "consistency_level": "Strong",
    }
    assert client.iterator.closed


@pytest.mark.asyncio
async def test_dry_run_reports_recoverable_authors_without_writing() -> None:
    writes: list[list[dict[str, object]]] = []

    async def fetcher(_ids: list[str]) -> list[dict[str, object]]:
        return [
            {"arxiv_id": "2601.00001", "authors": ["Ada Lovelace"]},
            {"arxiv_id": "2601.00002", "authors": []},
        ]

    stats = await backfill_ids(
        ["2601.00001", "2601.00002"],
        apply=False,
        batch_size=100,
        delay=0,
        fetcher=fetcher,
        writer=writes.extend,
    )

    assert (stats.recoverable, stats.updated, stats.unresolved_ids) == (
        1,
        0,
        ["2601.00002"],
    )
    assert writes == []


@pytest.mark.asyncio
async def test_apply_partially_updates_only_resolved_authors() -> None:
    writes: list[dict[str, object]] = []

    async def fetcher(_ids: list[str]) -> list[dict[str, object]]:
        return [{"arxiv_id": "2601.00001", "authors": ["Ada Lovelace"]}]

    stats = await backfill_ids(
        ["2601.00001"],
        apply=True,
        batch_size=100,
        delay=0,
        fetcher=fetcher,
        writer=writes.extend,
    )

    assert writes == [
        {
            "arxiv_id": "2601.00001",
            "authors": ["Ada Lovelace"],
            "_metadata_fields": {"authors"},
        }
    ]
    assert stats.updated == 1
