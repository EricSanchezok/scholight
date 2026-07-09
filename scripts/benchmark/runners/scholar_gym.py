"""ScholarGym Selection runner."""

from __future__ import annotations

import json
from statistics import mean
from typing import Any

from registry import BenchmarkSpec
from runners.base import BaseRunner, Query, QueryResult

_BENCH_FILE = "data/scholargym_bench.jsonl"
_AVG_DIST_CUTOFF = 100


class ScholarGymRunner(BaseRunner):
    """Evaluate ScholarGym selection metrics per official specification."""

    def __init__(self, spec: BenchmarkSpec, task_type: str) -> None:
        super().__init__(spec.output_root / "selection")
        self._data_dir = spec.data_dir

    def _load_queries(self) -> list[Query]:
        queries: list[Query] = []
        bench_path = self._data_dir / _BENCH_FILE
        with bench_path.open("r", encoding="utf-8") as f:
            for line in f:
                d = json.loads(line)
                queries.append(
                    Query(
                        id=d["qid"],
                        text=d["query"],
                        gt_arxiv_ids=[p["arxiv_id"] for p in d.get("cited_paper", [])],
                    )
                )
        return queries

    def _aggregate(self, results: list[QueryResult]) -> dict[str, Any]:
        recalls = [r.recall for r in results]
        precisions = [r.precision for r in results]

        f1s = [
            2 * r.precision * r.recall / (r.precision + r.recall)
            if (r.precision + r.recall) > 0
            else 0.0
            for r in results
        ]

        avg_distances = [_avg_distance(r) for r in results]
        gt_discards = [_gt_discard_rate(r) for r in results]

        return {
            "num_queries": len(results),
            "avg_recall": round(mean(recalls) if recalls else 0.0, 6),
            "avg_precision": round(mean(precisions) if precisions else 0.0, 6),
            "avg_f1": round(mean(f1s) if f1s else 0.0, 6),
            "avg_avg_distance": round(mean(avg_distances) if avg_distances else 0.0, 6),
            "avg_gt_discard_rate": round(mean(gt_discards) if gt_discards else 0.0, 6),
            "recall_distribution": {
                "max": round(max(recalls), 6) if recalls else 0.0,
                "min": round(min(recalls), 6) if recalls else 0.0,
            },
            "total_hits": sum(len(r.hit_ids) for r in results),
            "total_misses": sum(len(r.missed_ids) for r in results),
        }


def _avg_distance(r: QueryResult) -> float:
    gt = set(r.gt_arxiv_ids)
    if not gt:
        return 0.0
    pool_ids = r.pool_arxiv_ids or []
    total = 0.0
    for paper in gt:
        try:
            rank = pool_ids.index(paper) + 1
        except ValueError:
            rank = _AVG_DIST_CUTOFF + 1
        total += max(1.0 - rank / _AVG_DIST_CUTOFF, 0.0)
    return total / len(gt)


def _gt_discard_rate(r: QueryResult) -> float:
    gt = set(r.gt_arxiv_ids)
    pool = set(r.pool_arxiv_ids or [])
    selected = set(r.predicted_arxiv_ids)
    discarded = pool - selected
    if not discarded:
        return 0.0
    gt_discarded = (pool & gt) - selected
    return len(gt_discarded) / len(discarded)
