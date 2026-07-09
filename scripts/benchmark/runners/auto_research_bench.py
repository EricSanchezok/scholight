"""AutoResearchBench runner — supports wide and deep task types."""

from __future__ import annotations

import json
from statistics import mean
from typing import Any

from registry import BenchmarkSpec
from runners.base import BaseRunner, Query, QueryResult

_GT_FILE = "input_data/AutoResearchBench.jsonl"


class AutoResearchBenchRunner(BaseRunner):
    """Evaluate AutoResearchBench wide/deep — pure retrieval, no agent loop.

    *wide*: multi-answer (2-34 papers per query), metrics = IoU / Recall / Precision
    *deep*: single-answer (1 paper per query, 60 queries have no answer), metrics = hit_at_k / MRR
    """

    def __init__(self, spec: BenchmarkSpec, task_type: str) -> None:
        if task_type not in ("wide", "deep"):
            raise ValueError(f"Unknown task type: {task_type!r}.  Expected 'wide' or 'deep'.")
        super().__init__(spec.output_root / task_type)
        self._data_dir = spec.data_dir
        self._task_type = task_type

    def _load_queries(self) -> list[Query]:
        queries: list[Query] = []
        gt_path = self._data_dir / _GT_FILE
        with gt_path.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                d = json.loads(line)
                if d.get("type") != self._task_type:
                    continue
                queries.append(
                    Query(
                        id=f"{self._task_type}_{i:04d}",
                        text=d["question"],
                        gt_arxiv_ids=[str(aid) for aid in d.get("arxiv_id", [])],
                    )
                )
        return queries

    def _aggregate(self, results: list[QueryResult]) -> dict[str, Any]:
        if self._task_type == "deep":
            return self._aggregate_deep(results)
        return self._aggregate_wide(results)

    @staticmethod
    def _aggregate_wide(results: list[QueryResult]) -> dict[str, Any]:
        ious = [r.iou for r in results]
        recalls = [r.recall for r in results]
        precisions = [r.precision for r in results]
        return {
            "num_queries": len(results),
            "avg_iou": round(mean(ious) if ious else 0.0, 6),
            "avg_recall": round(mean(recalls) if recalls else 0.0, 6),
            "avg_precision": round(mean(precisions) if precisions else 0.0, 6),
            "iou_max": round(max(ious), 6) if ious else 0.0,
            "iou_min": round(min(ious), 6) if ious else 0.0,
            "total_hits": sum(len(r.hit_ids) for r in results),
            "total_misses": sum(len(r.missed_ids) for r in results),
        }

    @staticmethod
    def _aggregate_deep(results: list[QueryResult]) -> dict[str, Any]:
        # hit_at_k: fraction of queries where at least one ground-truth paper
        #           appeared in the engine's top-k results
        success_count = sum(1 for r in results if r.hit_ids)
        hit_at_k = success_count / len(results) if results else 0.0

        # MRR: mean of 1/rank for each query, where rank is position of first hit
        mrr_values: list[float] = []
        for r in results:
            for rank, aid in enumerate(r.predicted_arxiv_ids, 1):
                if aid in r.hit_ids:
                    mrr_values.append(1.0 / rank)
                    break
            else:
                mrr_values.append(0.0)

        return {
            "num_queries": len(results),
            "hit_at_k": round(hit_at_k, 6),
            "mrr": round(mean(mrr_values) if mrr_values else 0.0, 6),
            "num_queries_with_hits": success_count,
            "num_queries_without_hits": len(results) - success_count,
        }
