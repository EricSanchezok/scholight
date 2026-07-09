"""Multi-signal score fusion — SearchEngine Phase 3.

Extracts 5 complementary features per candidate from the Phase-2 retrieval
pool (~90 hits), z-score normalises each signal per-query, and fuses them
via a convex combination.  Pure NumPy, no disk I/O.

Features (validated on 400-wide benchmark):
  cosine          — embedding similarity to query       (pbs r=+.082)
  title_lexical   — query-title word overlap             (pbs r=+.071, 98% indep.)
  abstract_lexical — Jaccard between query and abstract  (new, orthogonal to cosine)
  term_density     — query-term concentration in abstract (new, favours focused papers)
  category_cohesion — category overlap with top-10 consensus (new, discrete signal)
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

import numpy as np

from scholight.config import settings

# ── Feature weights (tunable via scripts/benchmark/tune_fusion_weights.py) ───

_DEFAULT_WEIGHTS: dict[str, float] = {
    "cosine": 0.504986,
    "title_lexical": 0.327258,
    "abstract_lexical": 0.053399,
    "term_density": 0.078578,
    "category_cohesion": 0.035779,
}

_CATEGORY_TOP_K = 10


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════


def rerank_candidates(
    query: str,
    candidates: list[dict[str, Any]],
    query_vector: list[float] | None = None,
    weights: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Fuse multiple relevance signals; returns all candidates re-sorted.

    Does NOT truncate — the caller controls top-k.
    """
    n = len(candidates)
    if n <= 1:
        for c in candidates:
            c["l1_score"] = c.get("score", 0.0)
        return candidates

    w = weights or _DEFAULT_WEIGHTS

    embeds_norm = _normalised_embeddings(candidates)
    qv = _normalised_query_vector(query_vector) if query_vector else None

    features: dict[str, np.ndarray] = {}
    if embeds_norm is not None and qv is not None:
        features.update(_embedding_feature(embeds_norm, qv))
    features.update(_text_features(query, candidates))
    features.update(_category_features(candidates))

    fused = _fuse_features(features, w)

    # Abstract-length quality penalty: down-weight papers with very short,
    # low-signal abstracts while leaving normal ones untouched.
    _apply_length_penalty(fused, candidates)

    order = np.argsort(-fused)
    reranked = [candidates[int(i)] for i in order]
    for i, hit in zip(order, reranked):
        hit["l1_score"] = float(fused[int(i)])
        hit["score"] = float(fused[int(i)])

    return reranked


# ══════════════════════════════════════════════════════════════════════════════
# Vector helpers
# ══════════════════════════════════════════════════════════════════════════════


def _normalised_query_vector(qv: list[float]) -> np.ndarray:
    v = np.array(qv, dtype=np.float32)
    norm = np.linalg.norm(v)
    return v if norm == 0 else v / norm


def _normalised_embeddings(candidates: list[dict[str, Any]]) -> np.ndarray | None:
    emb = [c["abstract_embedding"] for c in candidates if c.get("abstract_embedding")]
    if len(emb) != len(candidates):
        return None
    mat = np.array(emb, dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return np.asarray(mat / norms)


# ══════════════════════════════════════════════════════════════════════════════
# Feature 1 — cosine (embedding space)
# ══════════════════════════════════════════════════════════════════════════════


def _embedding_feature(cand_norm: np.ndarray, qv: np.ndarray) -> dict[str, np.ndarray]:
    return {"cosine": cand_norm @ qv}


# ══════════════════════════════════════════════════════════════════════════════
# Features 2–4 — lexical (orthogonal to embedding, uses title + abstract text)
# ══════════════════════════════════════════════════════════════════════════════


def _text_features(
    query: str,
    candidates: list[dict[str, Any]],
) -> dict[str, np.ndarray]:
    n = len(candidates)
    q_tokens = set(query.lower().split())

    title_lex = np.zeros(n, dtype=np.float32)
    abstract_lex = np.zeros(n, dtype=np.float32)
    term_den = np.zeros(n, dtype=np.float32)

    for i, c in enumerate(candidates):
        title_tokens = set(c.get("title", "").lower().split())
        title_lex[i] = len(q_tokens & title_tokens) / max(len(q_tokens), 1)

        abstract_tokens = set(c.get("abstract", "").lower().split())
        if abstract_tokens:
            matched = len(q_tokens & abstract_tokens)
            abstract_lex[i] = matched / max(len(q_tokens), 1)
            term_den[i] = matched / len(abstract_tokens)

    return {
        "title_lexical": title_lex,
        "abstract_lexical": abstract_lex,
        "term_density": term_den,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Feature 5 — category (discrete signal, orthogonal to continuous spaces)
# ══════════════════════════════════════════════════════════════════════════════


def _category_features(candidates: list[dict[str, Any]]) -> dict[str, np.ndarray]:
    n = len(candidates)
    scores = np.array([c.get("score", 0.0) for c in candidates], dtype=np.float32)
    top_k = np.argsort(-scores)[: min(_CATEGORY_TOP_K, n)]

    cat_counter: Counter[str] = Counter()
    for idx in top_k:
        for cat in candidates[idx].get("categories") or []:
            cat_counter[cat] += 1

    if not cat_counter:
        return {"category_cohesion": np.zeros(n, dtype=np.float32)}

    total_top = len(top_k)
    consensus = {cat for cat, cnt in cat_counter.items() if cnt / total_top >= 0.3}

    cohesion = np.zeros(n, dtype=np.float32)
    if consensus:
        for i, c in enumerate(candidates):
            cat_set = set(c.get("categories") or [])
            if cat_set:
                cohesion[i] = len(cat_set & consensus) / max(len(cat_set | consensus), 1)

    return {"category_cohesion": cohesion}


# ══════════════════════════════════════════════════════════════════════════════
# Fusion
# ══════════════════════════════════════════════════════════════════════════════


def _fuse_features(
    features: dict[str, np.ndarray],
    weights: dict[str, float],
) -> np.ndarray:
    available = set(features.keys()) & set(weights.keys())
    if not available:
        n = len(next(iter(features.values())))
        return 1.0 / (1.0 + np.arange(n, dtype=np.float32))

    z_scores: list[np.ndarray] = []
    w_list: list[float] = []
    for key in sorted(available):
        v = features[key].astype(np.float64)
        mu, sigma = v.mean(), v.std()
        z = np.zeros_like(v) if sigma < 1e-8 else (v - mu) / sigma
        z_scores.append(z)
        w_list.append(weights[key])

    w_arr = np.array(w_list, dtype=np.float64)
    w_arr /= w_arr.sum()
    fused = np.zeros(len(z_scores[0]), dtype=np.float64)
    for w, z in zip(w_arr, z_scores):
        fused += w * z

    return fused.astype(np.float32)


# ══════════════════════════════════════════════════════════════════════════════
# Abstract-length quality penalty
# ══════════════════════════════════════════════════════════════════════════════


def _abstract_length_penalty(length: int) -> float:
    """Sigmoid penalty on log10 abstract length.

    weight = 1 / (1 + exp(-k * (log10(L) - log10(M))))

    where M = midpoint (default 120 chars), k = steepness (default 10.0).
    Abstracts under ~100 chars are heavily penalised; normal-length
    abstracts (>200 chars) are essentially unaffected.
    """
    k = settings.search_abstract_len_steepness
    m = settings.search_abstract_len_midpoint
    safe_len = max(length, 1)
    return 1.0 / (1.0 + math.exp(-k * (math.log10(safe_len) - math.log10(m))))


def _apply_length_penalty(fused: np.ndarray, candidates: list[dict[str, Any]]) -> None:
    """Multiply *fused* scores in-place by :func:`_abstract_length_penalty`."""
    for i, c in enumerate(candidates):
        abstract = c.get("abstract")
        length = len(abstract) if isinstance(abstract, str) else 0
        fused[i] *= _abstract_length_penalty(length)
