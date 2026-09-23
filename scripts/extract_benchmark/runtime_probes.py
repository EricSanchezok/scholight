"""Native B ownership, cookie isolation, retry and sustained overload probes."""

from __future__ import annotations

import http.client
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock
from urllib.parse import urlsplit

from checks import require

API = "93.184.216.3"
FIXTURE = "http://docs.extract.test:8000"
TOKEN = "isolated-benchmark-token-not-a-production-secret"  # nosec B105


def queue_evidence(log: str) -> dict:
    samples = []
    for line in log.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            samples.append(value)
    stages = {}
    for name, capacity, limit in (("Download", 2, 8), ("Parse", 1, 4), ("Browser", 1, 2)):
        rows = [r for r in samples if f"{name}Active" in r and f"{name}QueueDepth" in r]
        active = [r[f"{name}Active"] for r in rows]
        waiting = [r[f"{name}QueueDepth"] for r in rows]
        stages[name] = {
            "samples": len(rows),
            "max_active": max(active, default=None),
            "max_waiting": max(waiting, default=None),
            "passed": bool(rows)
            and min(active) >= 0
            and min(waiting) >= 0
            and max(active) <= capacity
            and max(waiting) <= limit
            and active[-1] == waiting[-1] == 0,
        }
    return {"stages": stages, "passed": all(r["passed"] for r in stages.values())}


def open_request(path: str, headers: dict | None = None) -> http.client.HTTPConnection:
    connection = http.client.HTTPConnection(API, 8001, timeout=10)
    connection.request(
        "POST",
        "/v1/extract",
        body=json.dumps({"url": FIXTURE + path, "render": "never", "headers": headers or {}}),
        headers={
            "Content-Type": "application/json",
            "X-Scholight-Internal-Token": TOKEN,
            "X-Scholight-Budget-Ms": "8000",
        },
    )
    return connection


def request(path: str, headers: dict | None = None) -> dict:
    started = time.monotonic()
    connection = open_request(path, headers)
    try:
        response = connection.getresponse()
        return {
            "status": response.status,
            "result": json.loads(response.read()),
            "seconds": time.monotonic() - started,
        }
    finally:
        connection.close()


def observations(path: str) -> list[dict]:
    connection = http.client.HTTPConnection("93.184.216.2", 8000, timeout=2)
    try:
        connection.request("GET", "/control/requests")
        return [r for r in json.loads(connection.getresponse().read()) if r["path"] == path]
    finally:
        connection.close()


def burst(path: str, count: int) -> list[dict]:
    barrier = Barrier(count)

    def call() -> dict:
        barrier.wait(timeout=5)
        return request(path)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return [f.result() for f in [pool.submit(call) for _ in range(count)]]


def semantics() -> None:
    evidence = []
    path = "/probe/delay?eight-callers"
    rows = burst(path, 8)
    require(all(r["status"] == 200 for r in rows), "Eight mergeable requests must all succeed")
    require(len(observations(path)) == 1, "Eight callers performed duplicate static downloads")
    require(len({r["result"]["content"] for r in rows}) == 1, "Shared content differs")
    evidence.append({"case": "eight_callers", "upstream_requests": 1, "responses": 8})

    path = "/probe/delay?cancel-one"
    leaving = open_request(path)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            remaining = pool.submit(request, path)
            time.sleep(0.1)
            leaving.close()
            require(
                remaining.result()["status"] == 200, "One caller cancelled the surviving caller"
            )
    finally:
        leaving.close()
    require(len(observations(path)) == 1, "Cancellation restarted shared work")
    evidence.append({"case": "independent_cancellation", "upstream_requests": 1})

    path = "/probe/private?separate-identities"
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(request, path, {"X-Fixture-Identity": x}) for x in ("alpha", "beta")]
        private = [f.result() for f in pending]
    for identity, row in zip(("alpha", "beta"), private, strict=True):
        require(
            row["status"] == 200 and identity in row["result"]["content"],
            "Credential isolation failed",
        )
        require(
            ("beta" if identity == "alpha" else "alpha") not in row["result"]["content"],
            "Cross-identity content leaked",
        )
    require(len(observations(path)) == 2, "Credentialed requests were incorrectly merged")
    evidence.append({"case": "credential_isolation", "upstream_requests": 2})

    first = request("/probe/cookie-start")
    second = request("/probe/cookie-read?next-request")
    require(first["status"] == second["status"] == 200, "Cookie probe failed")
    require(
        "fixture_session=one" in first["result"]["content"],
        "Cookie was not exercised within the redirect chain",
    )
    require(
        "cookie=none" in second["result"]["content"], "Pooled connection retained request cookies"
    )
    cookie_rows = [
        r
        for path in (
            "/probe/cookie-start",
            "/probe/cookie-read?redirect",
            "/probe/cookie-read?next-request",
        )
        for r in observations(path)
    ]
    require(
        len(cookie_rows) == 3 and len({tuple(r["peer"]) for r in cookie_rows}) == 1,
        "Cookie isolation did not exercise the same pooled connection",
    )
    evidence.append({"case": "pooled_cookie_isolation", "requests": 3, "connections": 1})

    path = "/probe/retry?public"
    retried = request(path)
    require(
        retried["status"] == 200 and len(observations(path)) == 2,
        "Public transient response was not retried exactly once",
    )
    require(retried["seconds"] >= 0.9, "Retry-After was ignored")
    private_retry = request("/probe/retry?private", {"X-Fixture-Identity": "alpha"})
    require(
        private_retry["status"] != 200 and len(observations("/probe/retry?private")) == 1,
        "Private request was retried",
    )
    evidence.append(
        {
            "case": "bounded_retry",
            "public_attempts": 2,
            "private_attempts": 1,
            "seconds": retried["seconds"],
        }
    )
    Path("/results/semantics.json").write_text(
        json.dumps({"passed": True, "evidence": evidence}, indent=2)
    )


def overload(seconds: float) -> None:
    require(seconds >= 60, "Sustained overload must run for at least 60 seconds")
    started = time.monotonic()
    lock = Lock()
    rows = []
    with Path("/results/overload.jsonl").open("w") as records:

        def work(worker: int) -> None:
            index = 0
            while time.monotonic() - started < seconds:
                before = time.monotonic()
                row = request(f"/probe/delay?overload-{worker}-{index}")
                row = {
                    "worker": worker,
                    "index": index,
                    "status": row["status"],
                    "seconds": row["seconds"],
                    "time": time.time(),
                }
                with lock:
                    rows.append(row)
                    records.write(json.dumps(row) + "\n")
                    records.flush()
                index += 1
                time.sleep(max(0, 0.1 - (time.monotonic() - before)))

        with ThreadPoolExecutor(max_workers=16) as pool:
            for future in [pool.submit(work, i) for i in range(16)]:
                future.result()
    success = sum(r["status"] == 200 for r in rows)
    rejected = sum(r["status"] == 503 for r in rows)
    report = {
        "seconds": time.monotonic() - started,
        "requests": len(rows),
        "success": success,
        "rejected": rejected,
        "max_response_seconds": max(r["seconds"] for r in rows),
    }
    report["passed"] = (
        success > 0
        and rejected > 0
        and success + rejected == len(rows)
        and report["max_response_seconds"] <= 3.2
    )
    Path("/results/overload-summary.json").write_text(json.dumps(report, indent=2))
    require(report["passed"], "Overload response/status gate failed; inspect retained raw outcomes")


if __name__ == "__main__":
    require(
        os.environ.get("SCHOLIGHT_BENCHMARK_CONTAINER") == "1", "Owned container marker required"
    )
    require(
        urlsplit(FIXTURE).hostname == "docs.extract.test", "Only the isolated fixture is allowed"
    )
    semantics() if sys.argv[4] == "semantics" else overload(float(sys.argv[1]))
