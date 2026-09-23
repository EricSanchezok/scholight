"""Report every outcome, comparable latency and explicit memory-soak gates."""

from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.startswith("{")]


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[max(0, math.ceil(fraction * len(values)) - 1)]


def latencies(rows: list[dict]) -> dict:
    successes = [r["latency_ms"] for r in rows if r["status"] == 200]
    return {
        "requests": len(rows),
        "statuses": dict(Counter(r["status"] for r in rows)),
        "quality_failures": sum(not r["quality"] for r in rows),
        "success_p50_ms": percentile(successes, 0.5),
        "success_p95_ms": percentile(successes, 0.95),
        "all_outcome_p95_ms": percentile([r["latency_ms"] for r in rows], 0.95),
        "rejected": sum(r["status"] == 503 for r in rows),
        "timed_out": sum(r["status"] in {0, 504} for r in rows),
        "mime_render_counts": dict(Counter(mime_render_group(r) for r in rows)),
    }


def mime_render_group(row: dict) -> str:
    result = row.get("result", {})
    mime = result.get("content_type", "unknown").partition(";")[0]
    rendered = "rendered" if result.get("rendered") else "static"
    return f"{mime}|{rendered}" if row["status"] == 200 else "unknown|failed"


def compare_outputs(before: list[dict], after: list[dict]) -> dict:
    """Compare paired supported requests; marker checks alone can miss content loss."""
    candidates = {(r["index"], r["case"]): r for r in after}
    mismatches = []
    schema_mismatches = failures = compared = 0
    for original in before:
        if original["status"] != 200:
            continue
        compared += 1
        candidate = candidates.get((original["index"], original["case"]))
        if candidate is None or candidate["status"] != 200:
            failures += 1
            mismatches.append(
                {"index": original["index"], "case": original["case"], "fields": ["status"]}
            )
            continue
        left, right = original["result"], candidate["result"]
        # These intentional A accounting corrections are reviewed independently.
        # Their wire types are still checked. No content or metadata is normalized.
        fields = sorted(
            key
            for key in left.keys() | right.keys()
            if key not in {"fetched_at", "source_bytes"} and left.get(key) != right.get(key)
        )
        schema = {key: type(value).__name__ for key, value in left.items()} != {
            key: type(value).__name__ for key, value in right.items()
        }
        schema_mismatches += schema
        if fields or schema:
            mismatches.append(
                {
                    "index": original["index"],
                    "case": original["case"],
                    "fields": fields,
                    "schema_changed": schema,
                }
            )
    return {
        "compared_supported_requests": compared,
        "supported_case_failures": failures,
        "schema_mismatches": schema_mismatches,
        "mismatches": mismatches,
        "gate": compared > 0 and not mismatches,
    }


def memory_report(rows: list[dict], samples: list[dict]) -> dict:
    if not samples:
        return {"complete": False}
    intervals = sorted((r["time"] - r["latency_ms"] / 1000 - 0.25, r["time"] + 0.25) for r in rows)
    merged: list[list[float]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    starts = [period[0] for period in merged]
    idle = []
    for sample in samples:
        index = bisect.bisect_right(starts, sample["time"]) - 1
        if index < 0 or sample["time"] > merged[index][1]:
            idle.append(sample)
    start, end = samples[0]["time"], samples[-1]["time"]
    first = [s["working_set"] for s in idle if start + 600 <= s["time"] < start + 4200]
    last = [s["working_set"] for s in idle if end - 3600 <= s["time"]]
    drift = statistics.median(last) - statistics.median(first) if first and last else None
    peak = max(s["working_set"] for s in samples)
    oom = max(s["oom_kill"] for s in samples) - samples[0]["oom_kill"]
    elapsed = end - start
    # Probe starts just before the client; tolerate only its one-second sampling granularity.
    complete = elapsed >= 14_399 and len(rows) >= 2000
    return {
        "elapsed_seconds": elapsed,
        "complete": complete,
        "peak_working_set_bytes": peak,
        "oom_kills": oom,
        "first_idle_samples": len(first),
        "last_idle_samples": len(last),
        "idle_median_growth_bytes": drift,
        "method": "Ignore request intervals plus 250ms; compare minutes10-70 with final hour.",
        "gate": complete
        and oom == 0
        and peak <= 640 * 1024 * 1024
        and drift is not None
        and drift <= 32 * 1024 * 1024,
    }


def summarize(directory: Path) -> dict:
    rows = read_rows(directory / "requests.jsonl")
    groups: dict[str, list[dict]] = defaultdict(list)
    mime_groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row["category"]].append(row)
        mime_groups[mime_render_group(row)].append(row)
    samples = read_rows(directory / "memory.jsonl")
    return {
        "directory": str(directory),
        "overall": latencies(rows),
        "categories": {key: latencies(value) for key, value in sorted(groups.items())},
        "mime_render": {key: latencies(value) for key, value in sorted(mime_groups.items())},
        "memory": memory_report(rows, samples),
    }


def matrix(directory: Path) -> dict:
    plan = json.loads((directory / "plan.json").read_text())
    reports = []
    by_scenario: dict[tuple[str, int, int], dict[str, list[dict]]] = defaultdict(dict)
    for item in plan:
        child = directory / item["name"]
        if not (child / "container-final.json").exists():
            reports.append({"name": item["name"], "complete": False})
            continue
        reports.append({"name": item["name"], "complete": True, **summarize(child)})
        scenario = (item["mode"], item["concurrency"], item["seed"])
        by_scenario[scenario][item["variant"]] = read_rows(child / "requests.jsonl")
    comparisons = []
    for (mode, concurrency, seed), variants in sorted(by_scenario.items()):
        if "baseline" not in variants:
            continue
        baseline = variants["baseline"]
        # Existing supported cases are explicit; failed PDFs remain in the overall report.
        supported = {row["case"] for row in baseline if row["status"] == 200}
        before = latencies([r for r in baseline if r["case"] in supported])
        p95 = before["success_p95_ms"]
        for name, rows in variants.items():
            if name == "baseline":
                continue
            after = latencies([r for r in rows if r["case"] in supported])
            later = after["success_p95_ms"]
            latency_gate = (
                p95 is not None and later is not None and later <= p95 + max(0.1 * p95, 50)
            )
            comparisons.append(
                {
                    "mode": mode,
                    "concurrency": concurrency,
                    "seed": seed,
                    "variant": name,
                    "comparable_case_count": len(supported),
                    "baseline": before,
                    "candidate": after,
                    "ordinary_latency_gate": latency_gate if concurrency == 1 else None,
                    "paired_outputs": compare_outputs(baseline, rows),
                    "note": "All errors/rejections remain in per-run reports; latency alone is not acceptance.",
                }
            )
    return {
        "runs": reports,
        "comparisons": comparisons,
        "complete": all(r["complete"] for r in reports),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    result = (
        matrix(args.directory)
        if (args.directory / "plan.json").exists()
        else summarize(args.directory)
    )
    (args.directory / "analysis.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))
