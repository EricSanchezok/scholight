"""Combine redacted observation windows without hiding gaps, restarts or canaries."""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime
from itertools import pairwise
from pathlib import Path

from checks import require
from observe import completion_summary, write_jsonl

MIB = 1024**2
FILES = ("completion", "memory", "lifecycle-errors")


def seconds(value: str) -> float:
    parsed = datetime.fromisoformat(value)
    require(parsed.tzinfo is not None, "Observation times require explicit time zones")
    return parsed.timestamp()


def coverage(intervals: list[tuple[float, float]], start: float, end: float) -> dict:
    require(end > start, "Selected observation interval must be positive")
    cursor = start
    gaps = []
    for left, right in sorted(intervals):
        left, right = max(start, left), min(end, right)
        if right <= left:
            continue
        if left > cursor:
            gaps.append((cursor, left))
        cursor = max(cursor, right)
    if cursor < end:
        gaps.append((cursor, end))
    missing = sum(right - left for left, right in gaps)
    return {
        "complete": not gaps,
        "requested_seconds": end - start,
        "covered_seconds": end - start - missing,
        "missing_seconds": missing,
        "gaps_unix_seconds": gaps,
    }


def memory_trends(rows: list[dict], completions: list[dict]) -> dict:
    activity: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in completions:
        if row.get("scope") == "internal" and "duration_ms" in row:
            end = seconds(row["timestamp"])
            activity[row["stream"]].append((end - row["duration_ms"] / 1000 - 0.25, end + 0.25))
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row["stream"]].append(row)
    streams = {}
    all_times = []
    for stream, events in groups.items():
        intervals = []
        for left, right in sorted(activity[stream]):
            if intervals and left <= intervals[-1][1]:
                intervals[-1] = (intervals[-1][0], max(right, intervals[-1][1]))
            else:
                intervals.append((left, right))
        starts = [left for left, _right in intervals]
        samples, idle = [], []
        parser = browser = None
        for row in sorted(events, key=lambda row: seconds(row["timestamp"])):
            parser = row.get("ParserActive", parser)
            browser = row.get("BrowserActive", browser)
            if "MemoryWorkingSet" not in row:
                continue
            when, size = seconds(row["timestamp"]), row["MemoryWorkingSet"]
            samples.append((when, size))
            i = bisect.bisect_right(starts, when) - 1
            if parser == 0 and browser == 0 and (i < 0 or when > intervals[i][1]):
                idle.append((when, size))
        if not samples:
            continue
        start, end = samples[0][0], samples[-1][0]
        all_times.extend(when for when, _size in samples)
        first = [size for when, size in idle if start + 600 <= when < start + 4200]
        last = [size for when, size in idle if when >= end - 3600]
        drift = statistics.median(last) - statistics.median(first) if first and last else None
        gaps = [right[0] - left[0] for left, right in pairwise(samples)]
        streams[stream] = {
            "samples": len(samples),
            "start": start,
            "end": end,
            "peak_working_set_bytes": max(size for _when, size in samples),
            "first_idle_samples": len(first),
            "last_idle_samples": len(last),
            "idle_median_growth_bytes": drift,
            "idle_growth_within_limit": drift <= 32 * MIB if drift is not None else None,
            "trend_has_disjoint_hours": end - start >= 7800,
            "trend_has_minimum_samples": min(len(first), len(last)) >= 60,
            "sampling_gap_count": sum(gap > 5 for gap in gaps),
            "largest_sampling_gap_seconds": max(gaps, default=None),
            "oom_kills_since_container_start": max(
                (r["MemoryOOMKills"] for r in events if "MemoryOOMKills" in r), default=None
            ),
        }
    all_times.sort()
    global_gaps = [right - left for left, right in pairwise(all_times)]
    oom_available = bool(streams) and all(
        s["oom_kills_since_container_start"] is not None for s in streams.values()
    )
    known_oom = [
        s["oom_kills_since_container_start"]
        for s in streams.values()
        if s["oom_kills_since_container_start"] is not None
    ]
    return {
        "available": bool(streams),
        "streams": streams,
        "peak_working_set_bytes": max(
            (s["peak_working_set_bytes"] for s in streams.values()), default=None
        ),
        "sampling_gap_count": sum(s["sampling_gap_count"] for s in streams.values()),
        "global_sampling_gap_count": sum(gap > 5 for gap in global_gaps),
        "largest_global_sampling_gap_seconds": max(global_gaps, default=None),
        "first_sample": all_times[0] if all_times else None,
        "last_sample": all_times[-1] if all_times else None,
        "cgroup_oom_counter_available": oom_available,
        "cgroup_oom_kills_since_container_start": sum(known_oom) if known_oom else None,
        "method": "Per task/log stream; exclude active worker gauges and internal request intervals plus 250ms. Compare minutes 10-70 with the final hour; never splice restarted tasks into one idle trend.",
    }


def task_summary(tasks: dict[str, dict]) -> dict:
    oom, unexplained_kills = [], []
    for name, task in tasks.items():
        reason = " ".join(
            [
                task.get("stoppedReason", ""),
                *(c.get("reason", "") for c in task.get("containers", [])),
            ]
        ).lower()
        if "outofmemory" in reason or "oom" in reason:
            oom.append(name)
        elif any(c.get("exitCode") == 137 for c in task.get("containers", [])):
            unexplained_kills.append(name)
    return {
        "observed_tasks": len(tasks),
        "oom_task_count": len(oom),
        "oom_tasks": oom,
        "unexplained_exit_137_tasks": unexplained_kills,
        "inventory": list(tasks.values()),
    }


def aggregate(
    directories: list[Path],
    *,
    start: str,
    end: str,
    canary_ids: set[str],
    completion_output: Path | None = None,
) -> dict:
    lower, upper = seconds(start), seconds(end)
    intervals, incomplete, inputs, resources, inventories = [], [], [], [], []
    records: dict[str, dict[tuple, dict]] = {name: {} for name in FILES}
    duplicates = dict.fromkeys(FILES, 0)
    tasks: dict[str, dict] = {}
    target = None
    for directory in sorted(set(directories)):
        window = json.loads((directory / "window.json").read_text())
        identity = tuple(window[key] for key in ("account", "region", "cluster"))
        require(target in (None, identity), "Cannot mix observations from different AWS targets")
        target = identity
        left, right = seconds(window["start"]), seconds(window["end"])
        require(right > left, "Invalid source observation interval")
        if right <= lower or left >= upper:
            continue
        required = [*(directory / (name + ".jsonl") for name in FILES)]
        required += [directory / "tasks.json", directory / "resources.json"]
        if not window["complete"] or not all(path.is_file() for path in required):
            incomplete.append(str(directory))
            continue
        require(
            right <= seconds(window["collected_at"]),
            "Source observation was collected before its window ended",
        )
        intervals.append((left, right))
        inputs.append(
            {
                "directory": str(directory),
                "window": window,
                "sha256": {
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in required
                },
            }
        )
        for name in FILES:
            with (directory / (name + ".jsonl")).open() as file:
                for line in file:
                    row = json.loads(line)
                    if not lower <= seconds(row["timestamp"]) <= upper:
                        continue
                    key = (row["stream"], row["event_id"], row.get("kind"))
                    if key in records[name]:
                        require(records[name][key] == row, "Conflicting duplicate event evidence")
                        duplicates[name] += 1
                    records[name][key] = row
        inventories.append(seconds(window["collected_at"]))
        for task in json.loads((directory / "tasks.json").read_text()):
            if task.get("stoppedAt") and seconds(task["stoppedAt"]) < lower:
                continue
            name = task["taskArn"]
            if name not in tasks or task.get("stoppedAt") or not tasks[name].get("stoppedAt"):
                tasks[name] = task
        resources.append(
            {
                "collected_at": window["collected_at"],
                "resources": json.loads((directory / "resources.json").read_text()),
            }
        )
    completions = sorted(records["completion"].values(), key=lambda row: seconds(row["timestamp"]))
    if completion_output is not None:
        require(not completion_output.exists(), "Preserve existing combined completion evidence")
        write_jsonl(completion_output, completions)
    inventories.sort()
    memory = memory_trends(list(records["memory"].values()), completions)
    return {
        "start": start,
        "end": end,
        "inputs": inputs,
        "incomplete_inputs": incomplete,
        "coverage": coverage(intervals, lower, upper),
        "deduplicated_events": duplicates,
        "completions": completion_summary(completions, canary_ids),
        "completion_counting_supported": memory["available"],
        "memory": memory,
        "lifecycle_error_events": len(
            {(r["stream"], r["event_id"]) for r in records["lifecycle-errors"].values()}
        ),
        "lifecycle_error_matches": list(records["lifecycle-errors"].values()),
        "tasks": task_summary(tasks),
        "largest_inventory_interval_seconds": max(
            (right - left for left, right in pairwise(inventories)), default=None
        ),
        "resource_snapshots": resources,
        "limitations": [
            "Log coverage is distinct from per-second memory continuity; inspect both and task changes.",
            "No observed OOM is not proof of zero OOM when stopped-task inventory or logs have gaps.",
            "Sparse or overlapping idle-hour samples cannot establish absence of sustained growth.",
            "This evidence report does not approve merge, rollout, rollback or final acceptance.",
        ],
    }


def canary_schedule(reports: list[dict], start: str, end: str) -> dict:
    lower, upper = seconds(start), seconds(end)
    runs = []
    for report in reports:
        if report.get("action") != "run" or not report["records"]:
            continue
        began = min(seconds(r["time"]) - r.get("duration_ms", 0) / 1000 for r in report["records"])
        finished = max(seconds(r["time"]) for r in report["records"])
        if finished >= lower and began <= upper:
            runs.append({"start": began, "end": finished, "passed": report["passed"]})
    runs.sort(key=lambda r: r["start"])
    occupied = {max(0, math.floor((r["start"] - lower) / 21600)) for r in runs}
    return {
        "runs": runs,
        "failed_runs": sum(not r["passed"] for r in runs),
        "six_hour_periods_without_run": [
            i for i in range(math.floor((upper - lower) / 21600) + 1) if i not in occupied
        ],
        "largest_start_interval_seconds": max(
            (right["start"] - left["start"] for left, right in pairwise(runs)), default=None
        ),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observation", type=Path, action="append", required=True)
    parser.add_argument("--canary-report", type=Path, action="append", default=[])
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--stage", choices=["A", "B"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    canaries = [json.loads(path.read_text()) for path in sorted(set(args.canary_report))]
    ids = {r["request_id"] for c in canaries for r in c["records"] if r.get("request_id")}
    report = aggregate(
        args.observation,
        start=args.start,
        end=args.end,
        canary_ids=ids,
        completion_output=args.output / "completion.jsonl",
    )
    report["canary_schedule"] = canary_schedule(canaries, args.start, args.end)
    elapsed = seconds(args.end) - seconds(args.start)
    natural = report["completions"]["natural_public_initial"]["requests"]
    report["observation_requirements"] = {
        "stage": args.stage,
        "elapsed_hours": elapsed / 3600,
        "minimum_duration_reached": elapsed >= (24 if args.stage == "A" else 72) * 3600,
        "natural_sample_sufficient": natural >= 100,
        "extend_for_natural_traffic": args.stage == "B" and natural < 100 and elapsed < 168 * 3600,
        "seven_day_limit_reached": elapsed >= 168 * 3600,
    }
    (args.output / "summary.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({"output": str(args.output), **report["observation_requirements"]}))
