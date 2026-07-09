#!/usr/bin/env python3
"""Tune three-way search weights — sweep, run, report.

Usage::

    # Full sweep (4 phases, ~11 runs, ~15 min)
    python scripts/benchmark/tune_weights.py

    # Quick check (3 hand-picked candidates)
    python scripts/benchmark/tune_weights.py --combs 0.4,0.35,0.25  0.5,0.3,0.2  0.33,0.33,0.34
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]

# ── Candidate grid ───────────────────────────────────────────────────────────

# Phase 1: single-signal ceiling
_P1 = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]

# Phase 2: two-signal synergy
_P2 = [(0.5, 0.5, 0.0), (0.5, 0.0, 0.5), (0.0, 0.5, 0.5)]

# Phase 3: three-signal grid across the simplex
_P3 = [
    (0.6, 0.2, 0.2),
    (0.33, 0.33, 0.34),
    (0.4, 0.35, 0.25),
    (0.3, 0.4, 0.3),
    (0.2, 0.5, 0.3),
]


def _grid() -> list[tuple[float, float, float, str]]:
    """Return (d, t, a, label) list from built-in phases."""
    out: list[tuple[float, float, float, str]] = []
    for phase, items in [("p1", _P1), ("p2", _P2), ("p3", _P3)]:
        for w in items:
            label = f"{phase}_d{w[0]:.2f}t{w[1]:.2f}a{w[2]:.2f}"
            out.append((w[0], w[1], w[2], label))
    return out


# ── Runner ────────────────────────────────────────────────────────────────────


def _parse_metrics(stdout: str) -> dict:
    """Extract the metrics JSON dict from benchmark stdout.

    The benchmark prints ``json.dumps(agg, indent=2)`` at the very end.
    We scan for outermost JSON blocks whose first key is ``num_queries``.
    """
    for m in re.finditer(r"\{[^{}]*\}", stdout):
        block = m.group()
        if '"num_queries"' in block:
            try:
                return json.loads(block)
            except json.JSONDecodeError:
                pass
    # Fallback: try with whitespace tolerance (indented JSON)
    for m in re.finditer(r"\{[^}]*\}", stdout, re.DOTALL):
        block = m.group()
        if '"num_queries"' in block:
            try:
                return json.loads(block)
            except json.JSONDecodeError:
                pass
    return {"avg_iou": 0.0, "avg_recall": 0.0, "_status": "failed"}


def _run_one(d: float, t: float, a: float, label: str) -> dict:
    """Set env vars, run benchmark, return metrics dict."""
    env = os.environ.copy()
    env["SCHOLIGHT_SEARCH_HYBRID_DENSE_WEIGHT"] = str(d)
    env["SCHOLIGHT_SEARCH_HYBRID_TITLE_WEIGHT"] = str(t)
    env["SCHOLIGHT_SEARCH_HYBRID_ABSTRACT_WEIGHT"] = str(a)

    cmd = [
        sys.executable,
        str(_HERE / "run.py"),
        "run",
        "autoresearchbench",
        "--type",
        "wide",
        "--top-k",
        "30",
        "--level",
        "1",
        "--version",
        label,
    ]

    t0 = time.perf_counter()
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(_REPO))
    elapsed = time.perf_counter() - t0

    metrics = _parse_metrics(result.stdout)
    metrics["_elapsed_s"] = round(elapsed, 1)

    if metrics.get("_status") == "failed":
        print()
        print("  [FAIL] no JSON in output — stderr tail:")
        for line in result.stderr.strip().splitlines()[-10:]:
            print(f"    {line}")

    return metrics


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Tune three-way search weights")
    parser.add_argument(
        "--combs",
        nargs="+",
        default=None,
        metavar="D,T,A",
        help="Weights to try, e.g. 0.4,0.35,0.25  0.5,0.3,0.2",
    )
    parser.add_argument("--phase", default=None, choices=["p1", "p2", "p3"])
    args = parser.parse_args()

    # Build candidate list
    if args.combs:
        candidates: list[tuple[float, float, float, str]] = []
        for c in args.combs:
            parts = [float(x) for x in c.split(",")]
            if len(parts) != 3:
                print(f"Error: {c!r} — expected 3 comma-separated floats")
                raise SystemExit(1)
            label = f"d{parts[0]:.2f}t{parts[1]:.2f}a{parts[2]:.2f}"
            candidates.append((parts[0], parts[1], parts[2], label))
    elif args.phase:
        phase_map = {"p1": _P1, "p2": _P2, "p3": _P3}
        weights_list = phase_map[args.phase]
        candidates = [
            (d, t, a, f"{args.phase}_d{d:.2f}t{t:.2f}a{a:.2f}") for d, t, a in weights_list
        ]
    else:
        candidates = _grid()

    print(f"Weight tuning — {len(candidates)} candidates\n")
    print(
        f"{'Label':^45s}  {'dense':>5s} {'title':>5s} {'abstr':>5s}  {'iou':>8s}  {'recall':>8s}  {'time':>6s}"
    )
    print("-" * 95)

    results: list[tuple[str, float, float, float]] = []

    for d, t, a, label in candidates:
        sys.stdout.write(f"  Running {label:40s} ...  ")
        sys.stdout.flush()
        metrics = _run_one(d, t, a, label)
        iou = metrics.get("avg_iou", 0.0)
        recall = metrics.get("avg_recall", 0.0)
        elapsed = metrics.get("_elapsed_s", 0.0)
        status = "OK" if metrics.get("_status") != "failed" else "FAIL"
        print(f"  iou={iou:.6f}  recall={recall:.6f}  {elapsed:.0f}s  {status}")
        results.append((label, iou, recall, d, t, a))

    # ── Summary ──
    print("\n" + "=" * 95)
    print(
        f"{'Rank':>5s}  {'Label':45s}  {'dense':>5s} {'title':>5s} {'abstr':>5s}  {'iou':>8s}  {'recall':>8s}"
    )
    print("-" * 95)
    sorted_results = sorted(results, key=lambda x: -x[1])
    for rank, (label, iou, recall, d, t, a) in enumerate(sorted_results, 1):
        marker = " ◀ BEST" if rank == 1 else ""
        print(
            f"  {rank:>2d}.  {label:45s}  {d:.2f}  {t:.2f}  {a:.2f}  {iou:.8f}  {recall:.6f}{marker}"
        )

    # Delta vs p1_dense-only baseline
    baseline = next((r for r in sorted_results if r[3] == 1.0 and r[4] == 0.0), None)
    best = sorted_results[0]
    if baseline:
        delta = best[1] - baseline[1]
        pct = (delta / baseline[1] * 100) if baseline[1] != 0 else 0
        print(f"\n  Baseline (dense-only): {baseline[1]:.6f}")
        print(f"  Best:                  {best[1]:.6f}  ({pct:+.1f}%)")


if __name__ == "__main__":
    main()
