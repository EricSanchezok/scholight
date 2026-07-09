"""Unit tests for Level 2 phases — ChunkSearch, MaxPAggregation, RRFFusion.

All tests run offline — no Milvus, no network.
"""

from unittest.mock import patch

import pytest

from scholight.models.search import SearchRequest
from scholight.search.base import PipelineContext
from scholight.search.level2.phases import (
    ChunkSearchPhase,
    MaxPAggregationPhase,
    RRFFusionPhase,
)


def _ctx(raw=None, chunks=None):
    return PipelineContext(
        request=SearchRequest(query="test"),
        query_vector=[0.0] * 1024,
        sparse_vector={1: 0.5},
        raw_hits=raw or [],
        chunk_hits=chunks or [],
    )


# ── ChunkSearchPhase — two-stage BM25→Dense ──


class TestChunkSearchPhase:
    @pytest.mark.asyncio
    async def test_two_stage_bm25_then_dense(self):
        """ChunkSearchPhase delegates to bm25_search_all_chunks + search_arxiv_chunks."""
        phase = ChunkSearchPhase()
        ctx = _ctx()
        bm25_fake = [{"chunk_id": "c1", "arxiv_id": "A"}]
        dense_fake = [{"chunk_id": "c1", "arxiv_id": "A", "score": 0.9}]

        with (
            patch("scholight.search.level2.phases._ensure_chunks_loaded"),
            patch(
                "scholight.search.level2.phases.bm25_search_all_chunks",
                return_value=bm25_fake,
            ),
            patch(
                "scholight.search.level2.phases.search_arxiv_chunks",
                return_value=dense_fake,
            ),
        ):
            await phase.execute(ctx)

        assert ctx.chunk_hits == dense_fake
        assert ctx.metadata["chunk_candidates"] == 1
        assert ctx.metadata["bm25_chunk_candidates"] == 1
        assert ctx.metadata["bm25_paper_candidates"] == 1
        assert ctx.metadata["chunk_mode"] == "bm25+dense"

    @pytest.mark.asyncio
    async def test_requires_query_vector(self):
        phase = ChunkSearchPhase()
        ctx = _ctx()
        ctx.query_vector = None
        with pytest.raises(ValueError, match="query_vector"):
            await phase.execute(ctx)


# ── MaxPAggregationPhase — C1 MaxP+SumP + C3 position weighting ──


class TestMaxPAggregationPhase:
    @pytest.mark.asyncio
    async def test_empty_chunks(self):
        phase = MaxPAggregationPhase()
        ctx = _ctx(chunks=[])
        await phase.execute(ctx)
        assert ctx.metadata["chunk_paper_scores"] == {}
        assert ctx.metadata["chunk_paper_count"] == 0

    @pytest.mark.asyncio
    async def test_maxp_sum_position(self):
        phase = MaxPAggregationPhase()
        ctx = _ctx(
            chunks=[
                {
                    "chunk_id": "c1",
                    "arxiv_id": "A",
                    "score": 0.9,
                    "heading": "Intro",
                    "content_text": "hello world",
                    "chunk_idx": 0,
                },
                {
                    "chunk_id": "c2",
                    "arxiv_id": "A",
                    "score": 0.7,
                    "heading": "Method",
                    "content_text": "the method",
                    "chunk_idx": 1,
                },
                {
                    "chunk_id": "c3",
                    "arxiv_id": "B",
                    "score": 0.3,
                    "heading": "X",
                    "content_text": "x",
                    "chunk_idx": 0,
                },
            ]
        )
        await phase.execute(ctx)
        s = ctx.metadata["chunk_paper_scores"]
        # C3 position boost: chunk_idx=1 gets +30% → 0.91
        # C1 α=0.5 blend: 0.5*max(0.91,0.9) + 0.5*avg(0.91,0.9) ≈ 0.9075
        assert s["A"] == pytest.approx(0.9075, abs=0.01)
        assert s["B"] == pytest.approx(0.3, abs=0.01)
        assert len(ctx.metadata["chunk_evidence"]["A"]) == 2
        assert ctx.metadata["chunk_paper_count"] == 2

    @pytest.mark.asyncio
    async def test_evidence_is_limited_to_top_3(self):
        """Evidence contains only {chunk_id, chunk_idx, score} per chunk, capped at 3."""
        phase = MaxPAggregationPhase()
        chunks = [
            {
                "chunk_id": f"c{i}",
                "arxiv_id": "X",
                "score": float(10 - i),
                "chunk_idx": i,
            }
            for i in range(5)
        ]
        ctx = _ctx(chunks=chunks)
        await phase.execute(ctx)
        evidence = ctx.metadata["chunk_evidence"]["X"]
        assert len(evidence) == 3
        # Evidence only contains chunk_id, chunk_idx, score (no content_text/heading)
        for entry in evidence:
            assert set(entry.keys()) == {"chunk_id", "chunk_idx", "score"}
            assert entry["score"] > 0
        # Best chunks first
        assert evidence[0]["chunk_id"] == "c0"


# ── RRFFusionPhase — C2 weighted RRF ──


class TestRRFFusionPhase:
    @pytest.mark.asyncio
    async def test_fusion_reorders_by_rrf(self):
        phase = RRFFusionPhase()
        papers = [
            {"arxiv_id": "A", "score": 0.50, "title": "A", "abstract": ""},
            {"arxiv_id": "B", "score": 0.10, "title": "B", "abstract": ""},
        ]
        ctx = _ctx(raw=papers)
        ctx.metadata["chunk_paper_scores"] = {"B": 0.99}
        await phase.execute(ctx)
        assert ctx.raw_hits[0]["arxiv_id"] == "B"

    @pytest.mark.asyncio
    async def test_chunk_only_papers_added(self):
        phase = RRFFusionPhase()
        ctx = _ctx(raw=[{"arxiv_id": "A", "score": 0.9, "title": "A", "abstract": ""}])
        ctx.metadata["chunk_paper_scores"] = {"B": 0.95}
        await phase.execute(ctx)
        assert len(ctx.raw_hits) == 2
        b = next(h for h in ctx.raw_hits if h["arxiv_id"] == "B")
        assert b["chunk_only"] is True
        assert ctx.metadata["rrf_chunk_only_papers"] == 1

    @pytest.mark.asyncio
    async def test_preserves_paper_score(self):
        phase = RRFFusionPhase()
        ctx = _ctx(raw=[{"arxiv_id": "A", "score": 0.88, "title": "X", "abstract": ""}])
        ctx.metadata["chunk_paper_scores"] = {}
        await phase.execute(ctx)
        assert ctx.raw_hits[0]["paper_score"] == 0.88
