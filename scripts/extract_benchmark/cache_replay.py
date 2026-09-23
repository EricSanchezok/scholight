"""Offline cache comparison over synthetic or explicitly anonymized real traces."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from datetime import datetime
from pathlib import Path

from cache_policies import LRU, Access, GreedyDualSize, WTinyLFU


def synthetic(kind: str, *, seed: int, count: int) -> list[Access]:
    rng = random.Random(seed)  # nosec B311
    keys = list(range(5000))
    hot = rng.choices(keys, weights=[1 / (i + 1) ** 1.1 for i in keys], k=count)
    sizes = [rng.choice([2048, 8192, 32_768, 131_072, 524_288, 2_097_152]) for _ in keys]
    result = []
    for index in range(count):
        if kind == "hotspot":
            key = hot[index]
        elif kind == "scan":
            key = hot[index] if index % 5 == 0 else 5000 + index
        else:
            key = (index // 16) % 2000
        size = sizes[key % len(sizes)]
        result.append(
            Access(
                hashlib.sha256(f"{seed}:{key}".encode()).hexdigest(),
                size,
                5 + size / 2000 + (key % 17) * 10,
                index / 10,
            )
        )
    return result


def real_trace(path: Path) -> tuple[list[Access], dict]:
    """Never read URLs; preserve observed order and forward only prior miss costs."""
    rows = []
    excluded = {"canary": 0, "private_or_missing_key": 0, "failure": 0, "unknown_cost": 0}
    for line in path.read_text().splitlines():
        item = json.loads(line)
        if "message" in item:
            try:
                item = json.loads(item["message"])
            except (TypeError, ValueError):
                continue
        if item.get("event") != "extract_completed" or item.get("scope") != "internal":
            continue
        if str(item.get("request_id", "")).startswith("canary-"):
            excluded["canary"] += 1
            continue
        key = item.get("cache_key_id")
        if (
            not item.get("cache_eligible")
            or not isinstance(key, str)
            or not re.fullmatch(r"[0-9a-f]{64}", key)
        ):
            excluded["private_or_missing_key"] += 1
            continue
        if item.get("outcome") not in {"static_success", "browser_success", "cache_hit"}:
            excluded["failure"] += 1
            continue
        timestamp = item["timestamp"]
        timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
        rows.append((timestamp, item))
    rows.sort(key=lambda row: row[0])
    costs: dict[str, float] = {}
    result = []
    origin = rows[0][0] if rows else 0
    for timestamp, item in rows:
        key = item["cache_key_id"]
        if not item["cache_hit"]:
            costs[key] = float(item["duration_ms"])
        if key not in costs:
            excluded["unknown_cost"] += 1
            continue
        result.append(Access(key, int(item["cache_entry_bytes"]), costs[key], timestamp - origin))
    return result, {
        "kind": "observed_successful_cache_eligible_completions",
        "trace_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "excluded": excluded,
        "limitations": [
            "Completion order, not an exact concurrent arrival-order replay.",
            "Miss cost is observed latency; hits reuse the most recent preceding miss cost.",
            "Failures and unpriced initial hits are censored and reported separately.",
            "Process restarts rotate opaque keys; cross-process reuse cannot be inferred.",
        ],
    }


def replay(trace: list[Access], capacity: int) -> dict:
    result = {}
    for policy in (LRU, WTinyLFU, GreedyDualSize):
        cache = policy(capacity=capacity)
        for access in trace:
            cache.access(access)
        result[policy.__name__] = cache.report()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, help="Redacted completion logs in JSONL")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=20_000)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--capacity", type=int, default=32 * 1024 * 1024)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    reports = []
    if args.trace:
        accesses, provenance = real_trace(args.trace)
        reports.append({"source": provenance, "policies": replay(accesses, args.capacity)})
    else:
        for seed in range(42, 42 + args.rounds):
            for kind in ("hotspot", "scan", "burst"):
                accesses = synthetic(kind, seed=seed, count=args.count)
                reports.append(
                    {
                        "source": {"kind": "synthetic", "workload": kind, "seed": seed},
                        "policies": replay(accesses, args.capacity),
                    }
                )
    report = {
        "capacity": args.capacity,
        "ttl": 600,
        "max_entries": 1024,
        "memory_model": "Trace retained bytes plus conservative policy entry/fixed overhead, not RSS.",
        "reports": reports,
    }
    (args.output / "comparison.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({"runs": len(reports), "output": str(args.output)}))
