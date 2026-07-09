"""Unit tests for scholight.search.common.aggregation — Level 2 chunk→paper fusion.

All four public functions are tested: aggregate_chunks, percentile_normalize,
fuse_paper_and_chunk, and rerank_with_chunks.  No Milvus or network required.
"""

from __future__ import annotations

from typing import Any

import pytest

from scholight.search.common.aggregation import (
    aggregate_chunks,
    fuse_paper_and_chunk,
    percentile_normalize,
    rerank_with_chunks,
)

# ══════════════════════════════════════════════════════════════════════════════
#  aggregate_chunks  →  dict[str, float]   (MaxP over top-n chunk scores)
# ══════════════════════════════════════════════════════════════════════════════


class TestAggregateChunks:
    """Per-paper MaxP aggregation of chunk-level retrieval hits."""

    def test_empty_returns_empty(self) -> None:
        """Empty input → empty dict."""
        assert aggregate_chunks([], top_n=5) == {}

    def test_single_paper_single_chunk(self) -> None:
        """One paper with one chunk → signal equals chunk score."""
        hits = [{"arxiv_id": "2101.00001", "score": 0.85}]
        result = aggregate_chunks(hits, top_n=5)
        assert result == {"2101.00001": pytest.approx(0.85)}

    def test_single_paper_multiple_chunks_top_n(self) -> None:
        """One paper, 3 chunks, top_n=2 → MaxP over best 2 = highest score."""
        hits = [
            {"arxiv_id": "2101.00001", "score": 0.45},
            {"arxiv_id": "2101.00001", "score": 0.92},
            {"arxiv_id": "2101.00001", "score": 0.61},
        ]
        result = aggregate_chunks(hits, top_n=2)
        # Max of top 2 sorted descending: [0.92, 0.61] → max = 0.92
        assert result == {"2101.00001": pytest.approx(0.92)}

    def test_multi_paper_chunks_interleaved(self) -> None:
        """Multiple papers with interleaved chunk hits, each gets MaxP."""
        hits = [
            {"arxiv_id": "2101.00001", "score": 0.80},
            {"arxiv_id": "2101.00002", "score": 0.60},
            {"arxiv_id": "2101.00001", "score": 0.95},
            {"arxiv_id": "2101.00002", "score": 0.88},
            {"arxiv_id": "2101.00003", "score": 0.72},
            {"arxiv_id": "2101.00003", "score": 0.55},
        ]
        result = aggregate_chunks(hits, top_n=5)
        assert len(result) == 3
        assert result["2101.00001"] == pytest.approx(0.95)
        assert result["2101.00002"] == pytest.approx(0.88)
        assert result["2101.00003"] == pytest.approx(0.72)

    def test_top_n_larger_than_available(self) -> None:
        """top_n=10 but paper only has 3 chunks → uses all 3 (MaxP still max)."""
        hits = [
            {"arxiv_id": "2101.00001", "score": 0.30},
            {"arxiv_id": "2101.00001", "score": 0.70},
            {"arxiv_id": "2101.00001", "score": 0.50},
        ]
        result = aggregate_chunks(hits, top_n=10)
        assert result == {"2101.00001": pytest.approx(0.70)}

    def test_hit_without_arxiv_id_skipped(self) -> None:
        """Hits missing arxiv_id are silently ignored."""
        hits: list[dict[str, Any]] = [
            {"score": 0.99},
            {"arxiv_id": "2101.00001", "score": 0.85},
        ]
        result = aggregate_chunks(hits, top_n=5)
        assert result == {"2101.00001": pytest.approx(0.85)}

    def test_hit_without_score_defaults_zero(self) -> None:
        """Hits missing 'score' key default to 0.0."""
        hits: list[dict[str, Any]] = [
            {"arxiv_id": "2101.00001"},
            {"arxiv_id": "2101.00001", "score": 0.80},
        ]
        result = aggregate_chunks(hits, top_n=5)
        assert result == {"2101.00001": pytest.approx(0.80)}


# ══════════════════════════════════════════════════════════════════════════════
#  percentile_normalize  →  list[float]  (percentile rank in [0,1])
# ══════════════════════════════════════════════════════════════════════════════


class TestPercentileNormalize:
    """Convert raw scores to percentile ranks."""

    def test_empty_list(self) -> None:
        """Empty input → empty output."""
        assert percentile_normalize([]) == []

    def test_single_value(self) -> None:
        """Single-element list → [0.0] (n ≤ 1 early-return)."""
        assert percentile_normalize([5.0]) == [0.0]

    def test_all_identical(self) -> None:
        """All identical scores → all 0.0 (zero strictly-less count)."""
        result = percentile_normalize([3.0, 3.0, 3.0, 3.0])
        assert result == pytest.approx([0.0, 0.0, 0.0, 0.0])

    def test_monotonic(self) -> None:
        """Higher raw score → higher or equal normalized rank."""
        raw = [0.9, 0.1, 0.5, 0.7, 0.3]
        result = percentile_normalize(raw)
        # For each pair, if raw[i] > raw[j], then result[i] >= result[j]
        for i in range(len(raw)):
            for j in range(len(raw)):
                if raw[i] > raw[j]:
                    assert result[i] >= result[j], (
                        f"raw[{i}]={raw[i]} > raw[{j}]={raw[j]} "
                        f"but result[{i}]={result[i]} < result[{j}]={result[j]}"
                    )

    def test_range_zero_to_one(self) -> None:
        """All outputs fall within [0, 1]."""
        for scores in (
            [1.0],
            [0.0, 1.0, 2.0],
            [-5.0, 0.0, 5.0, 10.0],
            [0.123, 0.456, 0.789],
            [100.0, 200.0, 300.0, 400.0, 500.0],
        ):
            result = percentile_normalize(scores)
            for v in result:
                assert 0.0 <= v <= 1.0, f"value {v} out of [0,1] for {scores=}"

    def test_typical_case(self) -> None:
        """Concrete values: [1,2,3,4,5] → [0.0, 0.25, 0.5, 0.75, 1.0]."""
        result = percentile_normalize([1.0, 2.0, 3.0, 4.0, 5.0])
        assert result == pytest.approx([0.0, 0.25, 0.5, 0.75, 1.0])

    def test_duplicates_share_rank(self) -> None:
        """Duplicate values receive the same percentile rank."""
        raw = [1.0, 3.0, 3.0, 5.0]
        result = percentile_normalize(raw)
        # raw: [1, 3, 3, 5] → sorted: [1, 3, 3, 5]
        # 1.0 has 0 strictly less → 0/3 = 0.0
        # 3.0 has 1 strictly less → 1/3 = 0.333...
        # 5.0 has 3 strictly less → 3/3 = 1.0
        assert result[0] == pytest.approx(0.0)  # 1.0
        assert result[1] == pytest.approx(1.0 / 3)  # 3.0
        assert result[2] == pytest.approx(1.0 / 3)  # 3.0 (same rank)
        assert result[3] == pytest.approx(1.0)  # 5.0


# ══════════════════════════════════════════════════════════════════════════════
#  fuse_paper_and_chunk  →  list[dict]  (percentile-rank convex combination)
# ══════════════════════════════════════════════════════════════════════════════


class TestFusePaperAndChunk:
    """Convex combination of percentile-normalised paper and chunk signals."""

    def test_empty_hits(self) -> None:
        """Empty input → empty output."""
        assert fuse_paper_and_chunk([], {}) == []

    def test_alpha_zero_uses_chunk_only(self) -> None:
        """alpha=0 -> fused = chunk_rank only; paper rank ignored entirely."""
        hits = [
            {"arxiv_id": "p1", "score": 0.9},
            {"arxiv_id": "p2", "score": 0.5},
            {"arxiv_id": "p3", "score": 0.3},
        ]
        chunk_signals = {"p1": 0.6, "p2": 0.8, "p3": 0.2}
        result = fuse_paper_and_chunk(hits, chunk_signals, alpha=0.0)

        # chunk_scores = [0.6, 0.8, 0.2]
        # percentile ranks: 0.6→0.5, 0.8→1.0, 0.2→0.0
        # fused = chunk_rank → [0.5, 1.0, 0.0]
        # sorted: p2(1.0), p1(0.5), p3(0.0)
        assert result[0]["arxiv_id"] == "p2"
        assert result[0]["score"] == pytest.approx(1.0)
        assert result[1]["arxiv_id"] == "p1"
        assert result[1]["score"] == pytest.approx(0.5)
        assert result[2]["arxiv_id"] == "p3"
        assert result[2]["score"] == pytest.approx(0.0)

    def test_alpha_one_uses_paper_only(self) -> None:
        """alpha=1 -> fused = paper_rank only; chunk signal ignored entirely."""
        hits = [
            {"arxiv_id": "p1", "score": 0.9},
            {"arxiv_id": "p2", "score": 0.5},
            {"arxiv_id": "p3", "score": 0.3},
        ]
        chunk_signals = {"p1": 0.6, "p2": 0.8, "p3": 0.2}
        result = fuse_paper_and_chunk(hits, chunk_signals, alpha=1.0)

        # paper_scores = [0.9, 0.5, 0.3]
        # percentile ranks: 0.9→1.0, 0.5→0.5, 0.3→0.0
        # fused = paper_rank → [1.0, 0.5, 0.0]
        # sorted: p1(1.0), p2(0.5), p3(0.0)
        assert result[0]["arxiv_id"] == "p1"
        assert result[0]["score"] == pytest.approx(1.0)
        assert result[1]["arxiv_id"] == "p2"
        assert result[1]["score"] == pytest.approx(0.5)
        assert result[2]["arxiv_id"] == "p3"
        assert result[2]["score"] == pytest.approx(0.0)

    def test_paper_without_chunks_unchanged(self) -> None:
        """Paper absent from chunk_signals gets chunk_rank = 0.0."""
        hits = [
            {"arxiv_id": "p1", "score": 0.9},
            {"arxiv_id": "p2", "score": 0.5},
        ]
        chunk_signals = {"p1": 0.8}  # p2 missing
        result = fuse_paper_and_chunk(hits, chunk_signals, alpha=0.4)

        # paper_scores: [0.9, 0.5] → ranks: [1.0, 0.0]
        # chunk_scores: [0.8, 0.0] → ranks: [1.0, 0.0]
        # fused = 0.4*[1.0, 0.0] + 0.6*[1.0, 0.0] = [1.0, 0.0]
        # sorted: p1(1.0), p2(0.0)
        assert len(result) == 2
        assert result[0]["arxiv_id"] == "p1"
        assert result[0]["score"] == pytest.approx(1.0)
        assert result[1]["arxiv_id"] == "p2"
        assert result[1]["score"] == pytest.approx(0.0)

    def test_alpha_04_typical_blend(self) -> None:
        """alpha=0.4 with known scores -> manually verified fused values."""
        hits = [
            {"arxiv_id": "p1", "score": 0.9},
            {"arxiv_id": "p2", "score": 0.5},
            {"arxiv_id": "p3", "score": 0.3},
        ]
        chunk_signals = {"p1": 0.6, "p2": 0.8, "p3": 0.2}
        result = fuse_paper_and_chunk(hits, chunk_signals, alpha=0.4)

        # paper_ranks:  0.9→1.0, 0.5→0.5, 0.3→0.0
        # chunk_ranks:  0.6→0.5, 0.8→1.0, 0.2→0.0
        # fused = 0.4*p + 0.6*c → [0.7, 0.8, 0.0]
        # sorted: p2(0.8), p1(0.7), p3(0.0)
        assert result[0]["arxiv_id"] == "p2"
        assert result[0]["score"] == pytest.approx(0.8)
        assert result[1]["arxiv_id"] == "p1"
        assert result[1]["score"] == pytest.approx(0.7)
        assert result[2]["arxiv_id"] == "p3"
        assert result[2]["score"] == pytest.approx(0.0)

    def test_output_sorted_descending(self) -> None:
        """Output always sorted by fused score from highest to lowest."""
        hits = [
            {"arxiv_id": "p1", "score": 0.5},
            {"arxiv_id": "p2", "score": 0.8},
            {"arxiv_id": "p3", "score": 0.3},
            {"arxiv_id": "p4", "score": 0.6},
        ]
        chunk_signals = {"p1": 0.7, "p2": 0.4, "p3": 0.9, "p4": 0.5}
        result = fuse_paper_and_chunk(hits, chunk_signals, alpha=0.5)
        scores = [h["score"] for h in result]
        assert scores == sorted(scores, reverse=True), f"not sorted descending: {scores}"

    def test_alpha_clamped(self) -> None:
        """alpha outside [0,1] is clamped to boundaries."""
        hits = [
            {"arxiv_id": "p1", "score": 0.9},
            {"arxiv_id": "p2", "score": 0.5},
        ]
        chunk_signals = {"p1": 0.8, "p2": 0.3}

        # α = -0.5 → clamped to 0.0 (chunk-only)
        result_neg = fuse_paper_and_chunk(hits, chunk_signals, alpha=-0.5)
        result_zero = fuse_paper_and_chunk(hits, chunk_signals, alpha=0.0)
        assert [h["score"] for h in result_neg] == pytest.approx([h["score"] for h in result_zero])

        # α = 1.5 → clamped to 1.0 (paper-only)
        result_big = fuse_paper_and_chunk(hits, chunk_signals, alpha=1.5)
        result_one = fuse_paper_and_chunk(hits, chunk_signals, alpha=1.0)
        assert [h["score"] for h in result_big] == pytest.approx([h["score"] for h in result_one])

    def test_uses_l1_score_when_present(self) -> None:
        """Paper signal prefers 'l1_score' over 'score' when available."""
        hits = [
            {"arxiv_id": "p1", "l1_score": 0.95, "score": 0.20},  # l1_score wins
            {"arxiv_id": "p2", "score": 0.50},  # only score
        ]
        # Only p1 in chunk_signals, p2 gets 0
        result = fuse_paper_and_chunk(hits, {"p1": 0.80}, alpha=1.0)

        # paper_scores: [0.95 (from l1_score), 0.50]
        # percentile ranks: 0.95→1.0, 0.50→0.0
        assert result[0]["arxiv_id"] == "p1"
        assert result[0]["score"] == pytest.approx(1.0)
        assert result[1]["arxiv_id"] == "p2"
        assert result[1]["score"] == pytest.approx(0.0)


# ══════════════════════════════════════════════════════════════════════════════
#  rerank_with_chunks  →  list[dict]  (full pipeline + length penalty)
# ══════════════════════════════════════════════════════════════════════════════


class TestRerankWithChunks:
    """Full Level-2 aggregation pipeline (aggregate → normalise → fuse → penalty → sort)."""

    def test_empty_papers_returns_empty(self) -> None:
        """Empty paper_hits → empty list (early return)."""
        assert rerank_with_chunks("query", [], []) == []

    def test_empty_chunks_keeps_order_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No chunk hits → chunk_signals all zero → sorting by fused preserves order."""
        monkeypatch.setattr(
            "scholight.search.common.aggregation._abstract_length_penalty",
            lambda x: 1.0,
        )
        hits = [
            {"arxiv_id": "p1", "score": 0.9, "abstract": "x" * 300},
            {"arxiv_id": "p2", "score": 0.5, "abstract": "x" * 300},
            {"arxiv_id": "p3", "score": 0.3, "abstract": "x" * 300},
        ]
        result = rerank_with_chunks("test query", hits, [], alpha=0.4, top_chunk_n=5)

        # No chunks → all chunk_ranks = 0 → fused = α * paper_rank
        # paper_ranks: 0.9→1.0, 0.5→0.5, 0.3→0.0
        # fused = 0.4 * [1.0, 0.5, 0.0] = [0.4, 0.2, 0.0]
        # penalty = 1.0 → no change → sort → p1(0.4), p2(0.2), p3(0.0)
        assert len(result) == 3
        assert result[0]["arxiv_id"] == "p1"
        assert result[1]["arxiv_id"] == "p2"
        assert result[2]["arxiv_id"] == "p3"

    def test_full_pipeline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """3 papers + interleaved chunks → verify fused scores and ordering."""
        monkeypatch.setattr(
            "scholight.search.common.aggregation._abstract_length_penalty",
            lambda x: 1.0,
        )

        paper_hits = [
            {"arxiv_id": "p1", "score": 0.9, "abstract": "x" * 300},
            {"arxiv_id": "p2", "score": 0.5, "abstract": "x" * 300},
            {"arxiv_id": "p3", "score": 0.3, "abstract": "x" * 300},
        ]
        chunk_hits = [
            {"arxiv_id": "p1", "score": 0.60},
            {"arxiv_id": "p1", "score": 0.45},
            {"arxiv_id": "p2", "score": 0.80},
            {"arxiv_id": "p2", "score": 0.70},
            {"arxiv_id": "p3", "score": 0.20},
        ]

        result = rerank_with_chunks(
            "machine learning", paper_hits, chunk_hits, alpha=0.4, top_chunk_n=5
        )

        # Step 1: aggregate_chunks → {"p1": 0.60, "p2": 0.80, "p3": 0.20}
        # Step 2-3: fuse_paper_and_chunk same as test_alpha_04_typical_blend
        #   paper_ranks: 0.9→1.0, 0.5→0.5, 0.3→0.0
        #   chunk_ranks: 0.6→0.5, 0.8→1.0, 0.2→0.0
        #   fused = 0.4*p + 0.6*c → [0.7, 0.8, 0.0]
        assert len(result) == 3
        assert result[0]["arxiv_id"] == "p2"
        assert result[0]["score"] == pytest.approx(0.8)
        assert result[1]["arxiv_id"] == "p1"
        assert result[1]["score"] == pytest.approx(0.7)
        assert result[2]["arxiv_id"] == "p3"
        assert result[2]["score"] == pytest.approx(0.0)

    def test_chunks_reorder_papers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Strong chunk signal can elevate a low-scoring paper above others."""
        monkeypatch.setattr(
            "scholight.search.common.aggregation._abstract_length_penalty",
            lambda x: 1.0,
        )

        paper_hits = [
            {"arxiv_id": "p1", "score": 0.98, "abstract": "x" * 300},
            {"arxiv_id": "p2", "score": 0.30, "abstract": "x" * 300},
        ]
        chunk_hits = [
            {"arxiv_id": "p1", "score": 0.10},  # weak chunks
            {"arxiv_id": "p2", "score": 0.95},  # very strong chunks
            {"arxiv_id": "p2", "score": 0.90},
        ]

        result = rerank_with_chunks(
            "deep learning", paper_hits, chunk_hits, alpha=0.4, top_chunk_n=5
        )

        # aggregate: p1→0.10, p2→0.95
        # paper_ranks: 0.98→1.0, 0.30→0.0
        # chunk_ranks: 0.10→0.0, 0.95→1.0
        # fused = 0.4*[1.0,0.0] + 0.6*[0.0,1.0] = [0.4, 0.6]
        # p2 (was score=0.30) now outranks p1 (was score=0.98)
        assert result[0]["arxiv_id"] == "p2"
        assert result[0]["score"] == pytest.approx(0.6)
        assert result[1]["arxiv_id"] == "p1"
        assert result[1]["score"] == pytest.approx(0.4)
