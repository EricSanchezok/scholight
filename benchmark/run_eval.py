#!/usr/bin/env python3
"""Hyperparameter grid evaluation for Scholight L1/L2 search.

Uses the Python SearchEngine API directly — no subprocess overhead.

Usage
-----
    python benchmark/run_eval.py --rounds l1     # L1 baseline only
    python benchmark/run_eval.py --rounds l2     # L2 all rounds
    python benchmark/run_eval.py --rounds 1      # L2 round 1 only (BM25 sweep)
    python benchmark/run_eval.py --rounds 1-3    # L2 rounds 1-3
    python benchmark/run_eval.py --rounds all    # L1 + L2 all rounds
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("run_eval")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

DATA_FILE = PROJECT_ROOT / "benchmark/autoresearchbench/input_data/AutoResearchBench.jsonl"
RESULTS_DIR = PROJECT_ROOT / "results"

DEEP_PASS_K = (1, 3, 5, 10)
WIDE_IOU_K = (1, 2, 4, 8, 16)


# ═════════════════════════════════════════════════════════════════════════
# Trial definitions
# ═════════════════════════════════════════════════════════════════════════


@dataclass
class Trial:
    name: str
    level: int
    top_k: int = 10
    env: dict[str, str] = field(default_factory=dict)


def _round1_l2() -> list[Trial]:
    return [
        Trial(
            f"bm25={bm25}_refine=1024",
            level=2,
            top_k=10,
            env={"SCHOLIGHT_BM25_COARSE_TOP_K": str(bm25), "SCHOLIGHT_DENSE_REFINE_TOP_K": "1024"},
        )
        for bm25 in [50, 100, 200, 300, 400]
    ]


def _round2_l2() -> list[Trial]:
    return [
        Trial(
            f"rrf_paper={pw:.1f}",
            level=2,
            top_k=10,
            env={
                "SCHOLIGHT_SEARCH_RRF_PAPER_WEIGHT": f"{pw:.1f}",
                "SCHOLIGHT_SEARCH_RRF_CHUNK_WEIGHT": f"{1.0 - pw:.1f}",
            },
        )
        for pw in [0.3, 0.4, 0.5, 0.6, 0.7]
    ]


def _round3_l2() -> list[Trial]:
    return [
        Trial(
            f"alpha={alpha:.2f}",
            level=2,
            top_k=10,
            env={"SCHOLIGHT_SEARCH_CHUNK_AGGREGATION_ALPHA": f"{alpha:.2f}"},
        )
        for alpha in [0.0, 0.25, 0.5, 0.75, 1.0]
    ]


def _round4_l2() -> list[Trial]:
    return [
        Trial(
            f"level=({pl},{cl})",
            level=2,
            top_k=10,
            env={"SCHOLIGHT_SEARCH_LEVEL": str(pl), "SCHOLIGHT_CHUNK_SEARCH_LEVEL": str(cl)},
        )
        for pl, cl in [(1, 1), (3, 1), (3, 3), (5, 3), (5, 5)]
    ]


L1_BASELINE = [Trial("baseline", level=1, top_k=20, env={})]

ALL_L2_ROUNDS: list[list[Trial]] = [
    [Trial("baseline", level=2, top_k=10, env={})],  # round 0
    _round1_l2(),  # round 1
    _round2_l2(),  # round 2
    _round3_l2(),  # round 3
    _round4_l2(),  # round 4
]


# ═════════════════════════════════════════════════════════════════════════
# arXiv ID normalization
# ═════════════════════════════════════════════════════════════════════════

_ARXIV_RE = re.compile(r"(\d{4}\.\d{4,5})")


def normalize_arxiv_id(raw: str) -> str:
    if not raw:
        return ""
    cleaned = re.sub(r"(?i)arxiv:", "", raw).strip()
    m = _ARXIV_RE.search(cleaned)
    return m.group(1) if m else cleaned.strip()


# ═════════════════════════════════════════════════════════════════════════
# Data loading
# ═════════════════════════════════════════════════════════════════════════


@dataclass
class QueryRecord:
    index: int
    question: str
    answer: list[str]
    arxiv_ids: set[str]
    query_type: str


def load_queries(path: str) -> list[QueryRecord]:
    records: list[QueryRecord] = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            d = json.loads(line)
            ids = {normalize_arxiv_id(aid) for aid in d.get("arxiv_id", []) if aid and aid.strip()}
            records.append(
                QueryRecord(
                    index=i,
                    question=d["question"],
                    answer=d.get("answer", []),
                    arxiv_ids=ids,
                    query_type=d.get("type", "deep"),
                )
            )
    return records


# ═════════════════════════════════════════════════════════════════════════
# Search execution (Python API — no subprocess)
# ═════════════════════════════════════════════════════════════════════════

_engine: Any = None


def _get_engine():
    global _engine
    if _engine is None:
        from scholight.search.engine import SearchEngine

        _engine = SearchEngine()
    return _engine


def _apply_env_overrides(env: dict[str, str]) -> dict[str, Any]:
    """Apply trial env overrides in-place. Returns snapshot dict for rollback."""
    from scholight.config import settings

    snapshot: dict[str, Any] = {}
    for key, val in env.items():
        field_name = key.removeprefix("SCHOLIGHT_").lower()
        if hasattr(settings, field_name):
            snapshot[field_name] = getattr(settings, field_name)
            try:
                parsed = type(snapshot[field_name])(val)
                object.__setattr__(settings, field_name, parsed)
            except (TypeError, ValueError):
                pass
    return snapshot


def _rollback_overrides(snapshot: dict[str, Any]) -> None:
    """Restore settings fields from snapshot."""
    if not snapshot:
        return
    from scholight.config import settings

    for field_name, original in snapshot.items():
        object.__setattr__(settings, field_name, original)


def run_search(query: str, trial: Trial) -> tuple[list[dict[str, Any]], float]:
    from scholight.models.search import SearchRequest

    snapshot = _apply_env_overrides(trial.env)
    try:
        engine = _get_engine()
        req = SearchRequest(query=query, top_k=trial.top_k, level=trial.level)
        result = asyncio.run(engine.search(req))
        hits = [h.model_dump() for h in result.hits]
        return hits, result.total_ms
    except Exception:
        logger.exception("SEARCH ERROR q=%s", query[:60])
        return [], 0.0
    finally:
        _rollback_overrides(snapshot)


# ═════════════════════════════════════════════════════════════════════════
# Deep search metrics
# ═════════════════════════════════════════════════════════════════════════


@dataclass
class DeepMetrics:
    accuracy_at_1: float = 0.0
    pass_at: dict[int, float] = field(default_factory=dict)
    mrr: float = 0.0


def evaluate_deep(
    hits: list[dict[str, Any]], gt_ids: set[str], k_values: tuple[int, ...] = DEEP_PASS_K
) -> DeepMetrics:
    if not gt_ids:
        return DeepMetrics(accuracy_at_1=1.0, pass_at=dict.fromkeys(k_values, 1.0), mrr=1.0)
    pred_ids = [normalize_arxiv_id(h.get("arxiv_id", "")) for h in hits]
    acc1 = 1.0 if pred_ids and pred_ids[0] in gt_ids else 0.0
    mrr = 0.0
    for rank, pid in enumerate(pred_ids, start=1):
        if pid in gt_ids:
            mrr = 1.0 / rank
            break
    pass_at = {k: 1.0 if any(pid in gt_ids for pid in pred_ids[:k]) else 0.0 for k in k_values}
    return DeepMetrics(accuracy_at_1=acc1, pass_at=pass_at, mrr=mrr)


# ═════════════════════════════════════════════════════════════════════════
# Wide search metrics
# ═════════════════════════════════════════════════════════════════════════


@dataclass
class WideMetrics:
    iou_at_k: dict[int, float] = field(default_factory=dict)
    recall_at_k: dict[int, float] = field(default_factory=dict)
    precision_at_k: dict[int, float] = field(default_factory=dict)


def _iou_recall_precision(
    gt_ids: set[str], pred_ids: list[str], k: int
) -> tuple[float, float, float]:
    top_k = set(pred_ids[:k])
    inter = len(gt_ids & top_k)
    union = len(gt_ids | top_k)
    return (
        inter / union if union else 0.0,
        inter / len(gt_ids) if gt_ids else 0.0,
        inter / len(top_k) if top_k else 0.0,
    )


def evaluate_wide(
    hits: list[dict[str, Any]], gt_ids: set[str], k_values: tuple[int, ...] = WIDE_IOU_K
) -> WideMetrics:
    pred_ids = [normalize_arxiv_id(h.get("arxiv_id", "")) for h in hits]
    pred_ids = [p for p in pred_ids if p]
    iou, rec, prec = {}, {}, {}
    for k_val in k_values:
        io, rl, pr = _iou_recall_precision(gt_ids, pred_ids, k_val)
        iou[k_val] = io
        rec[k_val] = rl
        prec[k_val] = pr
    return WideMetrics(iou_at_k=iou, recall_at_k=rec, precision_at_k=prec)


# ═════════════════════════════════════════════════════════════════════════
# Aggregation
# ═════════════════════════════════════════════════════════════════════════


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


@dataclass
class TrialResult:
    trial: Trial
    deep_count: int
    wide_count: int
    deep_acc1: float = 0.0
    deep_mrr: float = 0.0
    deep_pass: dict[int, float] = field(default_factory=dict)
    wide_iou: dict[int, float] = field(default_factory=dict)
    wide_recall: dict[int, float] = field(default_factory=dict)
    wide_precision: dict[int, float] = field(default_factory=dict)
    avg_ms: float = 0.0
    total_sec: float = 0.0

    def csv_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "trial": self.trial.name,
            "level": self.trial.level,
            "deep_count": self.deep_count,
            "wide_count": self.wide_count,
            "deep_acc1": f"{self.deep_acc1:.4f}",
            "deep_mrr": f"{self.deep_mrr:.4f}",
            "avg_ms": f"{self.avg_ms:.0f}",
            "total_sec": f"{self.total_sec:.0f}",
        }
        for k_val in sorted(self.deep_pass):
            row[f"deep_pass@{k_val}"] = f"{self.deep_pass[k_val]:.4f}"
        for k_val in sorted(self.wide_iou):
            row[f"wide_iou@{k_val}"] = f"{self.wide_iou[k_val]:.4f}"
        for k_val in sorted(self.wide_recall):
            row[f"wide_recall@{k_val}"] = f"{self.wide_recall[k_val]:.4f}"
        for k_val in sorted(self.wide_precision):
            row[f"wide_prec@{k_val}"] = f"{self.wide_precision[k_val]:.4f}"
        return row


def run_trial(trial: Trial, queries: list[QueryRecord]) -> TrialResult:
    deep_metrics: list[DeepMetrics] = []
    wide_metrics: list[WideMetrics] = []
    timings: list[float] = []
    n_deep = n_wide = 0
    total = len(queries)

    for i, rec in enumerate(queries):
        if i > 0 and i % 50 == 0:
            logger.info(
                "%s | %s  %s/%s  %s%%",
                time.strftime("%H:%M:%S"),
                trial.name,
                i,
                total,
                i * 100 // total,
            )

        hits, elapsed = run_search(rec.question, trial)
        timings.append(elapsed)

        if rec.query_type == "deep":
            n_deep += 1
            deep_metrics.append(evaluate_deep(hits, rec.arxiv_ids))
        else:
            n_wide += 1
            wide_metrics.append(evaluate_wide(hits, rec.arxiv_ids))

        if elapsed < 200:
            time.sleep(0.05)

    return TrialResult(
        trial=trial,
        deep_count=n_deep,
        wide_count=n_wide,
        deep_acc1=_mean([m.accuracy_at_1 for m in deep_metrics]),
        deep_mrr=_mean([m.mrr for m in deep_metrics]),
        deep_pass={k: _mean([m.pass_at.get(k, 0.0) for m in deep_metrics]) for k in DEEP_PASS_K},
        wide_iou={k: _mean([m.iou_at_k.get(k, 0.0) for m in wide_metrics]) for k in WIDE_IOU_K},
        wide_recall={
            k: _mean([m.recall_at_k.get(k, 0.0) for m in wide_metrics]) for k in WIDE_IOU_K
        },
        wide_precision={
            k: _mean([m.precision_at_k.get(k, 0.0) for m in wide_metrics]) for k in WIDE_IOU_K
        },
        avg_ms=_mean(timings),
        total_sec=sum(timings) / 1000,
    )


def write_csv(results: list[TrialResult], path: str) -> None:
    if not results:
        return
    first = results[0].csv_row()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(first.keys()))
        writer.writeheader()
        for r in results:
            writer.writerow(r.csv_row())


# ═════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════


def _fmt_trial(result: TrialResult) -> str:
    return (
        f"deep_acc1={result.deep_acc1:.4f}  deep_mrr={result.deep_mrr:.4f}  "
        f"wide_iou@16={result.wide_iou.get(16, 0):.4f}  "
        f"avg={result.avg_ms:.0f}ms  total={result.total_sec:.0f}s"
    )


def _parse_rounds_arg(rounds: str) -> tuple[list[Trial], list[list[Trial]]]:
    """Parse --rounds CLI arg into (l1_trials, l2_trial_sets)."""
    if rounds == "l1":
        return L1_BASELINE, []
    if rounds == "l2":
        return [], ALL_L2_ROUNDS
    if rounds in ("all", ""):
        return L1_BASELINE, ALL_L2_ROUNDS

    l1: list[Trial] = []
    l2: list[list[Trial]] = []
    for part in rounds.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = (int(n) for n in part.split("-"))
        else:
            lo = hi = int(part)
        for idx in range(lo, hi + 1):
            if idx == 0:
                l1 = L1_BASELINE
            elif 1 <= idx <= len(ALL_L2_ROUNDS):
                l2.append(ALL_L2_ROUNDS[idx - 1])
    return l1, l2


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="Scholight hyperparameter grid evaluation")
    p.add_argument("--rounds", type=str, default="all")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--output-dir", type=str, default=str(RESULTS_DIR))
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    )

    queries = load_queries(str(DATA_FILE))
    logger.info(
        "Loaded %s queries (%s deep, %s wide)",
        len(queries),
        sum(1 for q in queries if q.query_type == "deep"),
        sum(1 for q in queries if q.query_type == "wide"),
    )

    l1_trials, l2_trial_sets = _parse_rounds_arg(args.rounds)

    if args.dry_run:
        logger.info("=== L1 ===")
        for t in l1_trials:
            logger.info("  %s", t)
        logger.info("=== L2 ===")
        for ts in l2_trial_sets:
            for t in ts:
                logger.info("  %s", t)
        return

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if l1_trials:
        logger.info("══════ L1 ══════")
        l1_results: list[TrialResult] = []
        for trial in l1_trials:
            logger.info("L1 %s", trial.name)
            result = run_trial(trial, queries)
            l1_results.append(result)
            logger.info("  %s", _fmt_trial(result))
        write_csv(l1_results, str(out_dir / "l1_trials.csv"))

    if l2_trial_sets:
        logger.info("══════ L2 ══════")
        l2_results: list[TrialResult] = []
        for round_idx, trials in enumerate(l2_trial_sets):
            logger.info("── Round %s ──", round_idx)
            for trial in trials:
                logger.info("L2 %s", trial.name)
                result = run_trial(trial, queries)
                l2_results.append(result)
                logger.info("  %s", _fmt_trial(result))
        write_csv(l2_results, str(out_dir / "l2_trials.csv"))

    logger.info("Done.")


if __name__ == "__main__":
    main()
