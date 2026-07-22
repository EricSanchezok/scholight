"""Tests for Level 1 search phase behavior."""

from __future__ import annotations

from types import TracebackType
from unittest.mock import patch

import pytest

from scholight.config import Settings
from scholight.models.search import SearchRequest
from scholight.search.base import PipelineContext
from scholight.search.level1.phases import AnnSearchPhase, EmbedPhase


class StubQueryEmbedder:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def __aenter__(self) -> StubQueryEmbedder:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None

    async def embed_query(self, query: str) -> list[float]:
        self.queries.append(query)
        return [0.25, 0.5]


@pytest.mark.asyncio
async def test_embed_phase_uses_query_embedding() -> None:
    embedder = StubQueryEmbedder()
    context = PipelineContext(request=SearchRequest(query="deepseek"))

    with patch("scholight.search.level1.phases.Embedder", return_value=embedder):
        await EmbedPhase().execute(context)

    assert context.query_vector == [0.25, 0.5]
    assert embedder.queries == ["deepseek"]


def test_default_paper_candidate_pool_is_200() -> None:
    assert Settings.model_fields["search_paper_candidate_top_k"].default == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested_top_k", "expected_candidates"),
    [(5, 200), (200, 200), (250, 250)],
)
async def test_ann_search_applies_candidate_pool_floor(
    requested_top_k: int,
    expected_candidates: int,
) -> None:
    context = PipelineContext(
        request=SearchRequest(query="deepseek", top_k=requested_top_k),
        query_vector=[0.25, 0.5],
    )

    with patch(
        "scholight.search.level1.phases._do_paper_search",
        return_value=[],
    ) as paper_search:
        await AnnSearchPhase().execute(context)

    assert paper_search.call_args.kwargs["top_k"] == expected_candidates
