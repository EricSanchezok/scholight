"""Reproducible real-image corpus, latency and four-hour soak runner.

Run sequentially on the same machine. Only containers created by this invocation
are removed. The source image is never modified; no production endpoint is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json

# Isolated Docker CLI; never invokes a shell.
import subprocess  # nosec B404
import time
from pathlib import Path

from corpus import corpus

ROOT = Path(__file__).resolve().parents[2]
# Fixture-only value; this runner has no production access.
TOKEN = "isolated-benchmark-token-not-a-production-secret"  # nosec B105
ABLATIONS = {
    "parse-reuse": "SCHOLIGHT_EXTRACT_PARSE_REUSE",
    "singleflight": "SCHOLIGHT_EXTRACT_SINGLEFLIGHT",
    "queueing": "SCHOLIGHT_EXTRACT_QUEUEING",
    "connections": "SCHOLIGHT_EXTRACT_CONNECTION_REUSE",
}


def docker(*args: str) -> str:
    result = subprocess.check_output(["docker", *args], text=True)  # nosec
    return result.strip()


def run(
    image: str,
    output: Path,
    seconds: float,
    requests: int,
    seed: int,
    mode: str = "mixed",
    concurrency: int = 1,
    disable: str | None = None,
    http_version: str = "1.0",
) -> None:
    output.mkdir(parents=True, exist_ok=False)
    case_list = corpus()
    manifest = [
        {
            "name": c.name,
            "category": c.category,
            "expected": c.expected,
            "status": c.status,
            "sha256": hashlib.sha256(c.body).hexdigest(),
            "bytes": len(c.body),
        }
        for c in case_list
    ]
    metadata = {
        "image": json.loads(docker("image", "inspect", image))[0],
        "seconds": seconds,
        "requests": requests,
        "seed": seed,
        "mode": mode,
        "concurrency": concurrency,
        "disabled_feature": disable,
        "fixture_http_version": http_version,
        "harness_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((ROOT / "scripts/extract_benchmark").glob("*.py"))
        },
        "corpus": manifest,
    }
    (output / "manifest.json").write_text(json.dumps(metadata, indent=2))
    suffix = str(time.time_ns())
    network, fixture, app, client = [
        f"extract-bench-{name}-{suffix}" for name in ["net", "fixture", "app", "client"]
    ]
    created: list[str] = []
    probe = None
    try:
        docker("network", "create", "--internal", "--subnet", "93.184.216.0/24", network)
        created.append(network)
        mount = f"{ROOT / 'scripts/extract_benchmark'}:/benchmark:ro"
        docker(
            "run",
            "-d",
            "--no-healthcheck",
            "--name",
            fixture,
            "--network",
            network,
            "--ip",
            "93.184.216.2",
            "-e",
            f"EXTRACT_BENCH_HTTP_VERSION={http_version}",
            "-v",
            mount,
            "--entrypoint",
            "/app/.venv/bin/python",
            image,
            "/benchmark/fixture_server.py",
        )
        created.append(fixture)
        docker(
            "run",
            "-d",
            "--name",
            app,
            "--network",
            network,
            "--ip",
            "93.184.216.3",
            "--memory",
            "768m",
            "--memory-swap",
            "768m",
            "--memory-reservation",
            "256m",
            "--cpu-shares",
            "128",
            "-v",
            mount,
            "-e",
            "SCHOLIGHT_DISABLE_DOTENV=1",
            "-e",
            f"SCHOLIGHT_EXTRACT_INTERNAL_TOKEN={TOKEN}",
            "-e",
            "SCHOLIGHT_EXTRACT_STATIC_CONCURRENCY=2",
            "-e",
            "SCHOLIGHT_EXTRACT_BROWSER_CONCURRENCY=1",
            "-e",
            "SCHOLIGHT_EXTRACT_CACHE_MAX_BYTES=33554432",
            *(["-e", ABLATIONS[disable] + "=false"] if disable is not None else []),
            *(
                [
                    "-e",
                    "SCHOLIGHT_BENCHMARK_CONTAINER=1",
                    "--no-healthcheck",
                    "--entrypoint",
                    "/app/.venv/bin/python",
                ]
                if mode == "worker-faults"
                else []
            ),
            image,
            *(["/benchmark/worker_faults.py"] if mode == "worker-faults" else []),
        )
        created.append(app)
        (output / "containers.json").write_text(
            json.dumps({"app": app, "fixture": fixture, "client": client, "network": network})
        )
        if mode == "worker-faults":
            if int(docker("wait", app)):
                raise RuntimeError("Owned worker fault probe failed; inspect service.log")
            return
        for _ in range(90):
            try:
                docker(
                    "exec",
                    app,
                    "/app/.venv/bin/python",
                    "-c",
                    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/readyz', timeout=1)",
                )
                break
            except subprocess.CalledProcessError:
                time.sleep(1)
        else:
            raise RuntimeError("Extract did not become ready")
        with (output / "memory.jsonl").open("w") as memory:
            probe = subprocess.Popen(  # nosec
                ["docker", "exec", app, "/app/.venv/bin/python", "/benchmark/memory_probe.py"],
                stdout=memory,
                stderr=subprocess.STDOUT,
            )
            docker(
                "create",
                "--name",
                client,
                "--no-healthcheck",
                "--network",
                network,
                "--ip",
                "93.184.216.4",
                "-v",
                mount,
                "-v",
                f"{output.resolve()}:/results",
                "--entrypoint",
                "/app/.venv/bin/python",
                image,
                "/benchmark/http_faults.py" if mode == "faults" else "/benchmark/client.py",
                str(seconds),
                str(requests),
                str(seed),
                mode,
                str(concurrency),
            )
            created.append(client)
            docker("start", client)
            exit_code = int(docker("wait", client))
            (output / "client.log").write_text(docker("logs", client))
            if exit_code:
                raise RuntimeError(f"Benchmark client exited with status {exit_code}")
    finally:
        if probe is not None:
            probe.terminate()
            probe.wait(timeout=5)
        if app in created:
            (output / "container-final.json").write_text(docker("inspect", app))
            (output / "service.log").write_text(docker("logs", app))
        if fixture in created:
            (output / "fixture.log").write_text(docker("logs", fixture))
        for container in reversed(created[1:]):
            docker("rm", "-f", container)
        if network in created:
            docker("network", "rm", network)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=14_400)
    parser.add_argument("--requests", type=int, default=2400)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--mode",
        choices=["mixed", "cold", "warm", "duplicate", "short", "faults", "worker-faults"],
        default="mixed",
    )
    parser.add_argument("--concurrency", type=int, choices=[1, 2, 4, 8, 16], default=1)
    parser.add_argument("--disable", choices=sorted(ABLATIONS))
    parser.add_argument("--http-version", choices=["1.0", "1.1"], default="1.0")
    args = parser.parse_args()
    run(
        args.image,
        args.output,
        args.seconds,
        args.requests,
        args.seed,
        args.mode,
        args.concurrency,
        args.disable,
        args.http_version,
    )
