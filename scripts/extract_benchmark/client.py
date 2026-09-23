"""Load generator in its own cgroup, separate from Extract and fixture server."""

from __future__ import annotations

import json
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from corpus import corpus

FIXTURES = "http://93.184.216.2:8000"
API = "http://93.184.216.3:8001"
TOKEN = "isolated-benchmark-token-not-a-production-secret"  # nosec B105: fixture only


def run(seconds: float, requests: int, seed: int) -> None:
    case_list = corpus()
    with Path("/results/requests.jsonl").open("w") as records:
        rng = random.Random(seed)  # nosec B311: repeatable workload, not cryptography
        started = time.monotonic()
        for index in range(requests):
            delay = started + seconds * index / requests - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            case = case_list[index % len(case_list)]
            # A fixed hot subset and unique cold keys distinguish cache behavior.
            hot = rng.random() < 0.2
            query = "hot" if hot else f"cold-{index}"
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
                status, result = error.code, json.load(error)
            except (OSError, urllib.error.URLError, ValueError) as error:
                status, result = 0, {"error": type(error).__name__}
            text = str(result.get("content", ""))
            record = {
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
            records.write(json.dumps(record, ensure_ascii=False) + "\n")
            records.flush()
        remaining = seconds - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)


if __name__ == "__main__":
    run(float(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]))
