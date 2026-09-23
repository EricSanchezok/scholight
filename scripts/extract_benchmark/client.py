"""Load generator in its own cgroup, separate from Extract and fixture server."""

from __future__ import annotations

import json
import random
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from corpus import Case, corpus

FIXTURES = "http://93.184.216.2:8000"
API = "http://93.184.216.3:8001"
# Local fixture value, never used outside the isolated network.
TOKEN = "isolated-benchmark-token-not-a-production-secret"  # nosec B105


def request_one(case: Case, index: int, query: str, seed: int, hot: bool) -> dict[str, object]:
    payload = {"url": f"{FIXTURES}/{case.name}?{query}", "render": "auto"}
    before = time.monotonic()
    request = urllib.request.Request(
        API + "/v1/extract",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Scholight-Internal-Token": TOKEN,
            "X-Scholight-Request-Id": f"benchmark-{seed}-{index}",
        },
    )
    result: dict[str, object]
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310
            status, result = response.status, json.load(response)
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            result = json.load(error)
        except ValueError:
            result = {"error": "invalid_json"}
    except (OSError, urllib.error.URLError, ValueError) as error:
        status, result = 0, {"error": type(error).__name__}
    text = str(result.get("content", ""))
    return {
        "index": index,
        "time": time.time(),
        "case": case.name,
        "category": case.category,
        "hot": hot,
        "status": status,
        "latency_ms": (time.monotonic() - before) * 1000,
        "quality": status == case.status and all(t in text for t in case.expected),
        "result": result,
    }


def run(seconds: float, requests: int, seed: int, mode: str, concurrency: int) -> None:
    case_list = corpus()
    if mode == "warm":
        with Path("/results/warmup.jsonl").open("w") as warmup:
            for index, case in enumerate(case_list):
                warmup.write(json.dumps(request_one(case, index, "warm", seed, True)) + "\n")
    if mode == "duplicate":
        case_list = [case_list[0]]
    with Path("/results/requests.jsonl").open("w") as records:
        rng = random.Random(seed)  # nosec B311: repeatable workload, not cryptography
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for offset in range(0, requests, concurrency):
                delay = started + seconds * offset / requests - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                pending = []
                for index in range(offset, min(requests, offset + concurrency)):
                    case = case_list[index % len(case_list)]
                    hot = mode == "warm" or (mode == "mixed" and rng.random() < 0.2)
                    query = "warm" if mode == "warm" else "hot" if hot else f"cold-{index}"
                    if mode == "duplicate":
                        query = f"burst-{offset}"
                    pending.append(pool.submit(request_one, case, index, query, seed, hot))
                for future in pending:
                    records.write(json.dumps(future.result(), ensure_ascii=False) + "\n")
                    records.flush()
        remaining = seconds - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)


if __name__ == "__main__":
    run(float(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], int(sys.argv[5]))
