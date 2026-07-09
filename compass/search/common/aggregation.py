"""Chunk→paper score aggregation for Level 2 cross-layer search.

Groups chunk-level retrieval hits by paper, computes MaxP signal, percentile-rank
normalises both paper and chunk score distributions, and fuses them via convex
combination (Bruch et al., 2023).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np

from compass.search.common.fusion import _abstract_length_penalty

# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════


def aggregate_chunks(chunk_hits: list[dict[str, Any]], top_n: int = 5) -> dict[str, float]:
    """Per-paper chunk signal via MaxP over the top-*n* chunk scores.

    Returns ``{arxiv_id: max_chunk_score}`` — only papers with at least one
    chunk hit are present in the output.
    """
    if not chunk_hits:
        return {}

    grouped: dict[str, list[float]] = defaultdict(list)
    for hit in chunk_hits:
        aid = hit.get("arxiv_id")
        if aid is None:
            continue
        score = hit.get("score", 0.0)
        grouped[str(aid)].append(score)

    return {aid: max(sorted(scores, reverse=True)[:top_n]) for aid, scores in grouped.items()}


def percentile_normalize(scores: list[float]) -> list[float]:
    """Convert scores to percentile ranks in [0, 1].

    Each score is mapped to ``count(strictly less) / total``.  Identical
    scores receive the same rank; the highest score(s) get 1.0 only when
    they are strictly greater than every other score.
    """
    n = len(scores)
    if n <= 1:
        return [0.0] * n

    arr = np.array(scores, dtype=np.float64)
    order = np.argsort(arr)
    sorted_vals = arr[order]
    ranks = np.zeros(n, dtype=np.float64)

    i = 0
    while i < n:
        j = i
        while j < n and sorted_vals[j] == sorted_vals[i]:
            j += 1
        rank = i / (n - 1)  # i scores are strictly less
        ranks[order[i:j]] = rank
        i = j

    return ranks.tolist()


def fuse_paper_and_chunk(
    paper_hits: list[dict[str, Any]],
    chunk_signals: dict[str, float],
    alpha: float = 0.4,
) -> list[dict[str, Any]]:
    """Percentile-rank-normalise both signals and fuse via convex combination.

    ``fused = alpha * paper_rank + (1 - alpha) * chunk_rank``

    Returns *paper_hits* with updated ``"score"``, sorted descending by fused
    score.
    """
    if not paper_hits:
        return []

    alpha = max(0.0, min(1.0, alpha))

    # ── Collect raw signals ──────────────────────────────────────────
    paper_scores: list[float] = []
    chunk_scores: list[float] = []
    for hit in paper_hits:
        paper_scores.append(float(hit.get("l1_score", hit.get("score", 0.0))))
        chunk_scores.append(chunk_signals.get(str(hit.get("arxiv_id", "")), 0.0))

    # ── Percentile-rank normalise ────────────────────────────────────
    paper_ranks = percentile_normalize(paper_scores)
    chunk_ranks = percentile_normalize(chunk_scores)

    # ── Convex combination ───────────────────────────────────────────
    p = np.array(paper_ranks, dtype=np.float64)
    c = np.array(chunk_ranks, dtype=np.float64)
    fused = alpha * p + (1.0 - alpha) * c

    # ── Update scores & sort ─────────────────────────────────────────
    order = np.argsort(-fused)
    reranked: list[dict[str, Any]] = [paper_hits[int(i)] for i in order]
    for i, idx in enumerate(order):
        reranked[i]["score"] = float(fused[int(idx)])

    return reranked


def rerank_with_chunks(
    query: str,  # noqa: ARG001 — reserved for future feature use
    paper_hits: list[dict[str, Any]],
    chunk_hits: list[dict[str, Any]],
    alpha: float = 0.4,
    top_chunk_n: int = 5,
) -> list[dict[str, Any]]:
    """Full Level 2 aggregation pipeline.

    1. Group chunks → MaxP per paper
    2. PIT normalise paper scores and chunk signals
    3. Convex-combination fusion
    4. Abstract-length quality penalty (re-used from ``common.fusion``)
    5. Sort and return
    """
    if not paper_hits:
        return []

    alpha = max(0.0, min(1.0, alpha))

    # Step 1: chunk aggregation
    chunk_signals = aggregate_chunks(chunk_hits, top_n=top_chunk_n)

    # Steps 2–3: normalise + fuse
    reranked = fuse_paper_and_chunk(paper_hits, chunk_signals, alpha)

    # Step 4: abstract-length quality penalty
    _apply_chunk_aggregation_penalty(reranked)

    # Step 5: final sort (penalty may have re-ordered)
    reranked.sort(key=lambda h: h.get("score", 0.0), reverse=True)

    return reranked


# ══════════════════════════════════════════════════════════════════════════════
# Private helpers
# ══════════════════════════════════════════════════════════════════════════════


def _apply_chunk_aggregation_penalty(paper_hits: list[dict[str, Any]]) -> None:
    """Multiply each hit's ``score`` in-place by the abstract-length penalty."""
    for hit in paper_hits:
        abstract = hit.get("abstract")
        length = len(abstract) if isinstance(abstract, str) else 0
        hit["score"] = hit.get("score", 0.0) * _abstract_length_penalty(length)
