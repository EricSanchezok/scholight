#!/usr/bin/env python3
"""Benchmark runner — evaluate retrieval quality on external benchmarks.

Usage::

    python scripts/benchmark/run.py list
    python scripts/benchmark/run.py run autoresearchbench --type wide --top-k 10
    python scripts/benchmark/run.py run scholargym --type selection --top-k 10
    python scripts/benchmark/run.py diff autoresearchbench --type wide --level 1
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

_SELF_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SELF_DIR))

# fmt: off  — sys.path must be patched before these imports
from registry import get_spec, list_specs  # noqa: E402
from runners.base import BaseRunner  # noqa: E402

# fmt: on


def _bump_version(latest: str) -> str:
    """Bump minor version: v1.0 → v1.1, v1.9 → v1.10, v0.0 → v1.0."""
    if latest == "v0.0":
        return "v1.0"
    parts = latest.lstrip("v").split(".")
    major = int(parts[0])
    minor = int(parts[1]) + 1 if len(parts) > 1 else 0
    return f"v{major}.{minor}"


def _load_runner(key: str, task_type: str) -> BaseRunner:
    spec = get_spec(key)
    mod = importlib.import_module(spec.runner_module, package="runners")
    cls = getattr(mod, spec.runner_class)
    return cls(spec, task_type)


def _cmd_run(args: argparse.Namespace) -> None:
    spec = get_spec(args.benchmark)
    if args.type not in spec.types:
        print(
            f"Error: {args.benchmark} does not support type {args.type!r}. "
            f"Supported: {', '.join(spec.types)}"
        )
        raise SystemExit(1)

    runner = _load_runner(args.benchmark, args.type)
    version = args.version or _bump_version(runner.latest_version(args.level))
    print(
        f"Running {spec.name}  type={args.type}  level=l{args.level}  top_k={args.top_k}  version={version}"
    )

    agg = runner.run(
        top_k=args.top_k,
        version=version,
        max_queries=args.max_queries,
        level=args.level,
        concurrency=args.concurrency,
    )

    print()
    print(json.dumps(agg, indent=2, ensure_ascii=False))


def _cmd_list(args: argparse.Namespace) -> None:  # noqa: ARG001
    for s in list_specs():
        print(f"  {s.key:25s}  types={sorted(s.types)}  {s.name}")


def _cmd_diff(args: argparse.Namespace) -> None:
    spec = get_spec(args.benchmark)
    output_root = spec.output_root / args.type / f"l{args.level}"
    if not output_root.exists():
        print(f"No runs found for {args.benchmark}/{args.type}/l{args.level}.")
        return

    dirs = sorted(
        [d for d in output_root.iterdir() if d.is_dir() and d.name.startswith("v")],
        key=lambda d: tuple(int(x) for x in d.name.lstrip("v").split(".")),
    )
    if len(dirs) < 2:
        print(f"Need at least 2 versions to diff. Found: {[d.name for d in dirs]}")
        return

    prev, curr = dirs[-2], dirs[-1]
    prev_data = json.loads((prev / "results.json").read_text())
    curr_data = json.loads((curr / "results.json").read_text())

    prev_m = prev_data["metrics"]
    curr_m = curr_data["metrics"]

    print(f"\nDiff: {prev.name} → {curr.name}\n")
    for key in sorted(set(prev_m) | set(curr_m)):
        pv = prev_m.get(key)
        cv = curr_m.get(key)
        if isinstance(pv, (int, float)) and isinstance(cv, (int, float)):
            delta = cv - pv
            pct = f" ({delta / pv * 100:+.1f}%)" if pv != 0 else ""
            print(f"  {key:30s}  {pv:.6f} → {cv:.6f}{pct}")
        elif isinstance(pv, dict):
            print(f"  {key}:")
            for subk in pv:
                spv = pv.get(subk, 0)
                scv = cv.get(subk, 0) if isinstance(cv, dict) else 0
                if isinstance(spv, (int, float)) and isinstance(scv, (int, float)):
                    print(f"    {subk:28s}  {spv:.6f} → {scv:.6f}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run retrieval benchmarks against Compass SearchEngine."
    )
    sub = parser.add_subparsers(dest="command", help="Subcommand")

    # run
    p_run = sub.add_parser("run", help="Run a benchmark")
    p_run.add_argument("benchmark", help="Benchmark key (e.g. autoresearchbench)")
    p_run.add_argument("--type", required=True, help="Task type (e.g. wide, selection)")
    p_run.add_argument("--top-k", type=int, default=10, help="Results per query")
    p_run.add_argument(
        "--level",
        type=int,
        default=1,
        choices=[1, 2, 3],
        help="Search level (1=paper, 2=paper+chunk, 3=agent)",
    )
    p_run.add_argument("--concurrency", type=int, default=32, help="Max concurrent search requests")
    p_run.add_argument(
        "--oversample", type=int, default=None, help=argparse.SUPPRESS
    )  # deprecated — Engine hardcodes 3x
    p_run.add_argument(
        "--max-queries", type=int, default=None, help="Run only first N queries (smoke test)"
    )
    p_run.add_argument(
        "--version", default=None, help="Version label (auto-incremented if omitted)"
    )

    # list
    sub.add_parser("list", help="List available benchmarks")

    # diff
    p_diff = sub.add_parser("diff", help="Compare latest two benchmark runs")
    p_diff.add_argument("benchmark", help="Benchmark key")
    p_diff.add_argument("--type", required=True, help="Task type")
    p_diff.add_argument("--level", type=int, default=1, choices=[1, 2, 3], help="Search level")

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "run":
        _cmd_run(args)
    elif args.command == "list":
        _cmd_list(args)
    elif args.command == "diff":
        _cmd_diff(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
