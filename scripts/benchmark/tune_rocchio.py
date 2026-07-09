"""Random-search tuner for Rocchio hyperparameters.

.. deprecated::
    Rocchio parameters now live in ``compass/config.py`` Pydantic Settings
    (``COMPASS_SEARCH_ROCCHIO_*`` env vars).  The monkeypatch approach used
    by this script is no longer functional — ``refine()`` reads directly
    from ``settings`` instead of module globals.

    Use::
        COMPASS_SEARCH_ROCCHIO_POS_K=5 \
        COMPASS_SEARCH_ROCCHIO_MAX_TERMS=12 \
        uv run python scripts/benchmark/run.py run autoresearchbench --type wide

    This script is retained for reference until a config-based tuner is written.
"""

# ⚠️ ALL MODULES BELOW THIS POINT ARE OBSOLETE — see docstring above.

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

BENCHMARK_DIR = Path(__file__).resolve().parent.parent.parent / "benchmark" / "autoresearchbench"
GT_FILE = BENCHMARK_DIR / "input_data" / "AutoResearchBench.jsonl"
REFINER_PATH = (
    Path(__file__).resolve().parent.parent.parent / "compass" / "search" / "common" / "rocchio.py"
)
RESULT_PATH = BENCHMARK_DIR / ".rocchio_tune_results.json"

SEARCH_SPACE = {
    "pos_k": (2, 9),
    "neg_k": (2, 11),
    "alpha": (0.20, 0.80),
    "beta": (0.40, 1.20),
    "gamma": (0.0, 0.40),
    "pca_deflate": (0, 7),
    "hub_retain": (0.0, 0.50),
    "gate_threshold": (0.40, 0.70),
}


def _load_queries() -> list[tuple[str, str, set[str]]]:
    queries = []
    with GT_FILE.open() as f:
        for line in f:
            d = json.loads(line)
            if d.get("type") == "wide":
                queries.append((d["question"], set(d.get("arxiv_id", []))))
    return queries


def _sample(rng: np.random.Generator) -> dict[str, Any]:
    sp = SEARCH_SPACE
    return {
        "pos_k": int(rng.integers(*sp["pos_k"])),
        "neg_k": int(rng.integers(*sp["neg_k"])),
        "alpha": round(rng.uniform(*sp["alpha"]) * 20) / 20,
        "beta": round(rng.uniform(*sp["beta"]) * 20) / 20,
        "gamma": round(rng.uniform(*sp["gamma"]) * 20) / 20,
        "pca_deflate": int(rng.integers(*sp["pca_deflate"])),
        "hub_retain": round(rng.uniform(*sp["hub_retain"]) * 20) / 20,
        "gate_threshold": round(rng.uniform(*sp["gate_threshold"]) * 20) / 20,
    }


def _patch_file(params: dict[str, Any]) -> None:
    src = REFINER_PATH.read_text()
    mapping = {
        "_POS_K": "pos_k",
        "_NEG_K": "neg_k",
        "_ALPHA": "alpha",
        "_BETA": "beta",
        "_GAMMA": "gamma",
        "_PCA_DEFLATE": "pca_deflate",
        "_HUB_RETAIN": "hub_retain",
        "_GATE_THRESHOLD": "gate_threshold",
    }
    for const_name, key in mapping.items():
        src = re.sub(
            rf"^{re.escape(const_name)}\s*=\s*[^#\n]+",
            f"{const_name} = {params[key]}",
            src,
            flags=re.MULTILINE,
        )
    REFINER_PATH.write_text(src)


def _patch_runtime(params: dict[str, Any]) -> None:
    """Monkeypatch rocchio module globals so running SearchEngine picks them up."""
    import compass.search.common.rocchio as qr

    qr._PCA_DEFLATE = int(params["pca_deflate"])
    qr._POS_K = int(params["pos_k"])
    qr._NEG_K = int(params["neg_k"])
    qr._ALPHA = float(params["alpha"])
    qr._BETA = float(params["beta"])
    qr._GAMMA = float(params["gamma"])
    qr._HUB_RETAIN = float(params["hub_retain"])
    qr._GATE_THRESHOLD = float(params["gate_threshold"])


async def _evaluate(eval_queries: list[tuple[str, set[str]]]) -> float:
    from compass.models.search import SearchRequest
    from compass.search.engine import SearchEngine

    engine = SearchEngine()

    async def _one(q: str, gt: set[str]) -> float:
        req = SearchRequest(query=q, top_k=30, level=1)
        result = await engine.search(req)
        hits = len({h.arxiv_id for h in result.hits} & gt)
        return hits / len(gt) if gt else 0.0

    results = await asyncio.gather(*[_one(q, gt) for q, gt in eval_queries])
    return float(np.mean(results))


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


def main(trials: int = 200, eval_count: int = 30) -> None:
    all_queries = _load_queries()
    rng = np.random.default_rng(42)
    idx = rng.choice(len(all_queries), eval_count, replace=False)
    eval_queries = [(all_queries[i][0], all_queries[i][1]) for i in idx]

    history: list[dict] = []
    best_recall = -1.0
    best_params: dict[str, Any] = {}

    if RESULT_PATH.exists():
        history = json.loads(RESULT_PATH.read_text())
        if history:
            best_recall = max(r["recall"] for r in history)
            best_params = max(history, key=lambda r: r["recall"])["params"]
        print(f"Resuming from {len(history)} trials, best={best_recall:.6f}")

    start_idx = len(history)
    print(
        f"Trials {start_idx + 1}-{trials} on {eval_count} queries (~{eval_count * 0.8:.0f}s/trial)"
    )

    for i in range(start_idx, trials):
        t0 = time.perf_counter()
        params = _sample(rng)
        _patch_runtime(params)

        recall = asyncio.run(_evaluate(eval_queries))
        elapsed = time.perf_counter() - t0

        record = {
            "trial": i,
            "recall": round(recall, 6),
            "params": params,
            "elapsed_s": round(elapsed, 1),
        }
        history.append(record)

        if recall > best_recall:
            best_recall = recall
            best_params = params
            print(
                f"[{i + 1:3d}/{trials}] ★ NEW BEST recall={recall:.6f} (+{recall - 0.267:+.4f})  {elapsed:.0f}s"
            )
            print(f"  → {params}")
        else:
            if (i + 1) % 5 == 0:
                print(
                    f"[{i + 1:3d}/{trials}] best={best_recall:.6f}  last={recall:.6f}  {elapsed:.0f}s"
                )

        if (i + 1) % 5 == 0:
            RESULT_PATH.write_text(json.dumps(history, indent=2))

    RESULT_PATH.write_text(json.dumps(history, indent=2))
    _patch_file(best_params)

    print(f"\n{'=' * 60}")
    print(f"Best: {best_recall:.6f}  Params: {json.dumps(best_params)}")
    print(f"Applied to {REFINER_PATH}")


if __name__ == "__main__":
    t, q = 200, 30
    for a in sys.argv[1:]:
        if a.startswith("--trials="):
            t = int(a.split("=")[1])
        elif a.startswith("--queries="):
            q = int(a.split("=")[1])
    main(trials=t, eval_count=q)
