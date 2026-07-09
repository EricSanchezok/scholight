"""Offline weight tuner for fusion.

Pre-computes raw feature matrices for all benchmark queries (400 wide),
then searches weight-space via random sampling to maximise recall@30.

Usage:
    uv run python scripts/benchmark/tune_fusion_weights.py fetch
    uv run python scripts/benchmark/tune_fusion_weights.py tune
    uv run python scripts/benchmark/tune_fusion_weights.py apply
"""

from __future__ import annotations

import asyncio
import json
import pickle
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

# ── Constants ─────────────────────────────────────────────────────────────────

FEATURE_NAMES = [
    "cosine",
    "title_lexical",
    "abstract_lexical",
    "term_density",
    "category_cohesion",
]

BENCHMARK_DIR = Path(__file__).resolve().parent.parent.parent / "benchmark" / "autoresearchbench"
GT_FILE = BENCHMARK_DIR / "input_data" / "AutoResearchBench.jsonl"
FEATURES_CACHE = BENCHMARK_DIR / ".tune_features.pkl"
TOPK_CANDIDATES = 90
TOPK_EVAL = 30
_CATEGORY_TOP_K = 10

SCORE_FUSION_PATH = (
    Path(__file__).resolve().parent.parent.parent / "scholight" / "search" / "common" / "fusion.py"
)


# ═══════════════════════════════════════════════════════════════════════════════
# Feature extraction (thin wrapper over score_fusion internals)
# ═══════════════════════════════════════════════════════════════════════════════


def _normalised_embeddings(candidates: list[dict[str, Any]]) -> np.ndarray | None:
    emb = [c["abstract_embedding"] for c in candidates if c.get("abstract_embedding")]
    if len(emb) != len(candidates):
        return None
    mat = np.array(emb, dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def _extract_features(
    query: str,
    candidates: list[dict[str, Any]],
    qv: np.ndarray,
) -> dict[str, np.ndarray] | None:
    """Extract all 5 feature vectors for one query."""
    cand_norm = _normalised_embeddings(candidates)
    if cand_norm is None:
        return None

    qv_norm = qv / (np.linalg.norm(qv) or 1.0)
    n = cand_norm.shape[0]

    # Feature 1: cosine
    cosine = cand_norm @ qv_norm

    # Features 2–4: lexical
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

    # Feature 5: category
    scores = np.array([c.get("score", 0.0) for c in candidates], dtype=np.float32)
    top_idx = np.argsort(-scores)[: min(_CATEGORY_TOP_K, n)]
    cat_counter: Counter[str] = Counter()
    for idx in top_idx:
        for cat in candidates[idx].get("categories") or []:
            cat_counter[cat] += 1
    total_top = len(top_idx)
    consensus = {cat for cat, cnt in cat_counter.items() if cnt / total_top >= 0.3}
    cohesion = np.zeros(n, dtype=np.float32)
    if consensus:
        for i, c in enumerate(candidates):
            cat_set = set(c.get("categories") or [])
            if cat_set:
                cohesion[i] = len(cat_set & consensus) / max(len(cat_set | consensus), 1)

    return {
        "cosine": cosine,
        "title_lexical": title_lex,
        "abstract_lexical": abstract_lex,
        "term_density": term_den,
        "category_cohesion": cohesion,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 1: fetch + compute features
# ═══════════════════════════════════════════════════════════════════════════════


def _load_wide_queries() -> list[tuple[str, str, set[str]]]:
    queries: list[tuple[str, str, set[str]]] = []
    with GT_FILE.open() as f:
        for i, line in enumerate(f):
            d = json.loads(line)
            if d.get("type") != "wide":
                continue
            qid = f"wide_{i:04d}"
            gt = {str(aid) for aid in d.get("arxiv_id", [])}
            queries.append((qid, d["question"], gt))
    return queries


async def fetch_features(concurrency: int = 8) -> None:
    from scholight.pipeline.embedder import Embedder
    from scholight.store.client import get_client
    from scholight.store.fields import PAPER_SEARCH_WITH_EMBEDDING
    from scholight.store.query import hybrid_search_arxiv_papers

    queries = _load_wide_queries()
    print(f"Loaded {len(queries)} wide queries")

    get_client()
    print("Milvus connected")

    sem = asyncio.Semaphore(concurrency)
    all_features: dict[str, dict[str, np.ndarray]] = {}
    all_arxiv_ids: dict[str, list[str]] = {}
    all_gt: dict[str, set[str]] = {}
    fail_count = 0

    async def _process_one(qid: str, text: str, gt: set[str]) -> None:
        nonlocal fail_count
        async with sem:
            try:
                async with Embedder() as emb:
                    qv_raw = await emb.embed_single(text)
                qv = np.array(qv_raw, dtype=np.float32)
                raw = hybrid_search_arxiv_papers(
                    query_vector=qv_raw,
                    top_k=TOPK_CANDIDATES,
                    output_fields=list(PAPER_SEARCH_WITH_EMBEDDING),
                )
                feats = _extract_features(text, raw, qv)
                if feats is not None:
                    all_features[qid] = feats
                all_arxiv_ids[qid] = [r.get("arxiv_id", "") for r in raw[:TOPK_CANDIDATES]]
                all_gt[qid] = gt
            except Exception:
                fail_count += 1

    pending = {asyncio.create_task(_process_one(qid, text, gt)) for qid, text, gt in queries}
    total = len(queries)
    done_count = 0

    while pending:
        done, pending = await asyncio.wait(pending, timeout=10)
        for _ in done:
            done_count += 1
            if done_count % 50 == 0 or done_count == total:
                print(f"  {done_count}/{total} queries ({fail_count} failed)")

    print(f"Features extracted: {len(all_features)}/{total} queries ({fail_count} failed)")

    if fail_count > total // 2:
        print("ERROR: too many failed queries — aborting")
        sys.exit(1)

    cache = {
        "features": all_features,
        "arxiv_ids": all_arxiv_ids,
        "ground_truth": all_gt,
        "feature_names": FEATURE_NAMES,
        "top_k": TOPK_EVAL,
    }
    FEATURES_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with FEATURES_CACHE.open("wb") as f:
        pickle.dump(cache, f)
    print(f"Cache saved to {FEATURES_CACHE} ({FEATURES_CACHE.stat().st_size / 1024:.0f} KB)")


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 2: tune weights
# ═══════════════════════════════════════════════════════════════════════════════


def _zscore_fuse(features: dict[str, np.ndarray], weights: dict[str, float]) -> np.ndarray:
    available = set(features) & set(weights)
    if not available:
        n = len(next(iter(features.values())))
        return np.zeros(n, dtype=np.float32)

    fused = np.zeros(len(next(iter(features.values()))), dtype=np.float64)
    w_total = 0.0
    for key in available:
        v = features[key].astype(np.float64)
        mu, sigma = v.mean(), v.std()
        z = (v - mu) / sigma if sigma > 1e-8 else np.zeros_like(v)
        fused += weights[key] * z
        w_total += weights[key]
    return (fused / w_total).astype(np.float32)


def _eval_weights(
    weights: dict[str, float],
    features_dict: dict[str, dict[str, np.ndarray]],
    arxiv_ids_dict: dict[str, list[str]],
    gt_dict: dict[str, set[str]],
    top_k: int = TOPK_EVAL,
) -> float:
    total_recall = 0.0
    n = 0
    for qid, feats in features_dict.items():
        gt = gt_dict.get(qid)
        arxiv_ids = arxiv_ids_dict.get(qid)
        if gt is None or arxiv_ids is None:
            continue
        fused = _zscore_fuse(feats, weights)
        order = np.argsort(-fused)
        pred = {arxiv_ids[int(i)] for i in order[:top_k]}
        hits = len(pred & gt)
        total_recall += hits / len(gt) if gt else 0.0
        n += 1
    return total_recall / n if n > 0 else 0.0


def _random_weights() -> dict[str, float]:
    raw = np.random.dirichlet(np.ones(len(FEATURE_NAMES)))
    return {name: float(raw[i]) for i, name in enumerate(FEATURE_NAMES)}


def tune(trials: int = 5000) -> None:
    if not FEATURES_CACHE.exists():
        print(f"Features cache not found at {FEATURES_CACHE}")
        print("Run `fetch` first.")
        sys.exit(1)

    print(f"Loading features from {FEATURES_CACHE}...")
    t0 = time.perf_counter()
    with FEATURES_CACHE.open("rb") as f:
        cache = pickle.load(f)
    print(f"  loaded {len(cache['features'])} queries in {(time.perf_counter() - t0) * 1000:.0f}ms")

    features_dict: dict[str, dict[str, np.ndarray]] = cache["features"]
    arxiv_ids_dict: dict[str, list[str]] = cache["arxiv_ids"]
    gt_dict: dict[str, set[str]] = cache["ground_truth"]

    # Import current defaults for comparison
    from scholight.search.common.fusion import _DEFAULT_WEIGHTS

    current = {k: v for k, v in _DEFAULT_WEIGHTS.items() if k in FEATURE_NAMES}
    total = sum(current.values())
    current = {k: v / total for k, v in current.items()}

    cur_recall = _eval_weights(current, features_dict, arxiv_ids_dict, gt_dict)
    print(f"Current defaults: recall@30 = {cur_recall:.6f}")

    uniform = {name: 1.0 / len(FEATURE_NAMES) for name in FEATURE_NAMES}
    baseline = _eval_weights(uniform, features_dict, arxiv_ids_dict, gt_dict)
    print(f"Uniform baseline: recall@30 = {baseline:.6f}")

    print(f"\nRandom search ({trials} trials)...")
    best_recall = cur_recall
    best_weights = current
    t_start = time.perf_counter()

    for trial_idx in range(trials):
        w = _random_weights()
        r = _eval_weights(w, features_dict, arxiv_ids_dict, gt_dict)
        if r > best_recall:
            best_recall = r
            best_weights = w
        if (trial_idx + 1) % 1000 == 0:
            elapsed = time.perf_counter() - t_start
            print(
                f"  {trial_idx + 1}/{trials}  best={best_recall:.6f}  rate={trial_idx / elapsed:.0f} trials/s"
            )

    elapsed = time.perf_counter() - t_start
    print(f"\nSearch completed in {elapsed:.1f}s ({trials / elapsed:.0f} trials/s)")
    print(f"\n{'=' * 60}")
    print(f"Best recall@30:       {best_recall:.6f}")
    print(f"Uniform baseline:      {baseline:.6f}")
    print(f"Current defaults:      {cur_recall:.6f}")
    print(f"Gain over current:     {best_recall - cur_recall:+.6f}")
    print("\nBest weights:")
    for name in FEATURE_NAMES:
        w = best_weights.get(name, 0.0)
        bar = "█" * int(w * 40)
        print(f"  {name:22s} {w:.4f}  {bar}")

    result_path = FEATURES_CACHE.with_suffix(".best_weights.json")
    result_path.write_text(
        json.dumps(
            {
                "recall": round(best_recall, 6),
                "baseline_uniform": round(baseline, 6),
                "current_defaults": round(cur_recall, 6),
                "weights": {k: round(v, 6) for k, v in best_weights.items()},
                "trials": trials,
            },
            indent=2,
        )
    )
    print(f"\nBest weights saved to {result_path}")


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 3: apply
# ═══════════════════════════════════════════════════════════════════════════════


def apply_best() -> None:
    result_path = FEATURES_CACHE.with_suffix(".best_weights.json")
    if not result_path.exists():
        print(f"No best weights found at {result_path}")
        print("Run `tune` first.")
        sys.exit(1)

    result = json.loads(result_path.read_text())
    best = result["weights"]
    total = sum(best.values())

    lines = ["_DEFAULT_WEIGHTS: dict[str, float] = {"]
    for name in FEATURE_NAMES:
        w = round(best.get(name, 0.0) / total, 6)
        lines.append(f'    "{name}": {w},')
    lines.append("}")

    new_block = "\n".join(lines)

    import re

    src = SCORE_FUSION_PATH.read_text()
    pattern = r"_DEFAULT_WEIGHTS: dict\[str, float\] = \{.*?\n\}"
    new_src = re.sub(pattern, new_block, src, flags=re.DOTALL)
    if new_src == src:
        print("ERROR: could not find _DEFAULT_WEIGHTS in fusion.py")
        sys.exit(1)

    SCORE_FUSION_PATH.write_text(new_src)
    print(f"Applied best weights to {SCORE_FUSION_PATH}")
    print(f"Previous recall: {result['current_defaults']:.6f}")
    print(f"Expected recall:  {result['recall']:.6f}")
    print(f"Gain:             {result['recall'] - result['current_defaults']:+.6f}")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: tune_fusion_weights.py <fetch|tune|apply>")
        sys.exit(1)

    cmd = sys.argv[1]
    trials = 5000
    concurrency = 8
    for arg in sys.argv[2:]:
        if arg.startswith("--trials="):
            trials = int(arg.split("=")[1])
        elif arg.startswith("--concurrency="):
            concurrency = int(arg.split("=")[1])

    if cmd == "fetch":
        asyncio.run(fetch_features(concurrency=concurrency))
    elif cmd == "tune":
        tune(trials=trials)
    elif cmd == "apply":
        apply_best()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
