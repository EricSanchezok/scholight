"""Tests for Level 1 search phase behavior."""

from __future__ import annotations

from types import TracebackType
from unittest.mock import patch

import pytest

from scholight.models.search import SearchRequest
from scholight.search.base import PipelineContext
from scholight.search.level1.phases import EmbedPhase


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
