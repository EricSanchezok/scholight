"""Measure real download and Chromium envelopes on the owned fixture network."""

from __future__ import annotations

import asyncio
import gc
import json
import math
import os
import platform
import sys
import time
from pathlib import Path

from checks import require

from scholight.models.web_extract import ExtractResponseFormat, RenderMode
from scholight.web_extract import runtime  # noqa: F401
from scholight.web_extract.engine import ExtractInput
from scholight.web_extract.fetcher import HttpFetcher
from scholight.web_extract.isolated import IsolatedBrowser
from scholight.web_extract.memory import read_cgroup
from scholight.web_extract.process_family import enable_subreaping
from scholight.web_extract.spool import Spool
from scholight.web_extract.worker_supervisor import WorkerSupervisor

MIB = 1024 * 1024


async def measure(operation) -> dict:
    baseline = peak = read_cgroup().working_set
    started = time.monotonic()
    task = asyncio.create_task(operation)
    stopped = False
    try:
        while not task.done():
            peak = max(peak, read_cgroup().working_set)
            if peak >= 640 * MIB or time.monotonic() - started > 45:
                stopped = True
                task.cancel()
                break
            await asyncio.sleep(0.01)
        result = await asyncio.gather(task, return_exceptions=True)
        peak = max(peak, read_cgroup().working_set)
        failed = isinstance(result[0], BaseException)
        size = 0
        if not failed:
            size = result[0].source_bytes
            result[0].close()
        return {
            "baseline": baseline,
            "peak": peak,
            "peak_delta": max(0, peak - baseline),
            "completed": not failed,
            "stopped_by_probe": stopped,
            "output_bytes": size,
            "error_type": type(result[0]).__name__ if failed else None,
            "elapsed_seconds": time.monotonic() - started,
        }
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def run() -> None:
    require(
        sys.platform == "linux"
        and platform.machine() in {"arm64", "aarch64"}
        and sys.version_info[:2] == (3, 11),
        "Requires native Linux ARM64 / Python 3.11",
    )
    require(
        os.environ.get("SCHOLIGHT_BENCHMARK_CONTAINER") == "1", "Owned container marker required"
    )
    require(
        int(Path("/sys/fs/cgroup/memory.max").read_text()) == 768 * MIB,
        "Requires 768 MiB hard limit",
    )
    enable_subreaping()
    spool = Spool(Path("/data/phase-calibration"))
    spool.start()
    parser, browser = WorkerSupervisor("parser"), WorkerSupervisor("browser")
    adapter = IsolatedBrowser(browser, spool, max_content_bytes=50_000_000)
    rows = []
    try:
        await parser.warmup()
        await browser.warmup()
        with Path("/results/phase-measurements.jsonl").open("w") as records:
            for trial in range(5):
                for kind, sizes in (
                    ("download", (8192, MIB, 8 * MIB, 32 * MIB, 49_000_000)),
                    ("browser", (100, 1000, 5000, 15_000)),
                ):
                    for size in sizes:
                        fetcher = HttpFetcher(spool=spool, concurrency=2, reuse_connections=True)
                        try:
                            if kind == "browser":
                                await browser.close()
                                await browser.warmup()
                            path = (
                                f"calibration/{'download' if kind == 'download' else 'dom'}/{size}"
                            )
                            request = ExtractInput(
                                "http://93.184.216.2:8000/" + path,
                                RenderMode.NEVER if kind == "download" else RenderMode.ALWAYS,
                                ExtractResponseFormat.MAIN_MARKDOWN,
                            )
                            gc.collect()
                            row = await measure(
                                fetcher.fetch(request)
                                if kind == "download"
                                else adapter.render(request)
                            )
                            row.update({"round": trial, "kind": kind, "input_bytes_or_nodes": size})
                            rows.append(row)
                            records.write(json.dumps(row) + "\n")
                            records.flush()
                            require(
                                row["completed"] and not row["stopped_by_probe"],
                                "Calibration failed or reached its guard; inspect partial evidence",
                            )
                            require(
                                spool.reserved_bytes == 0, "Calibration leaked a spool reservation"
                            )
                        finally:
                            await fetcher.close()
    finally:
        await asyncio.gather(parser.close(), browser.close())
        spool.close()
    downloads = [r for r in rows if r["kind"] == "download"]
    download_fixed = (
        math.ceil(
            (1.5 * max(r["peak_delta"] for r in downloads if r["output_bytes"] <= 65_536) + 8 * MIB)
            / MIB
        )
        * MIB
    )
    report = {
        "python": sys.version,
        "machine": platform.machine(),
        "rounds": 5,
        "sample_interval_seconds": 0.01,
        "download": {
            "fixed_bytes": download_fixed,
            "per_input_byte": max(
                1,
                math.ceil(
                    max(
                        (1.5 * r["peak_delta"] - download_fixed) / max(1, r["output_bytes"])
                        for r in downloads
                    )
                ),
            ),
        },
        "browser": {
            "fixed_bytes": math.ceil(
                (1.5 * max(r["peak_delta"] for r in rows if r["kind"] == "browser") + 8 * MIB) / MIB
            )
            * MIB,
            "per_input_byte": 0,
        },
        "limitations": [
            "Finite owned document corpus, not a bound on arbitrary JavaScript, assets or compressed documents.",
            "Measured phase deltas include idle sibling workers; validate combined workloads and retained caches in the soak.",
        ],
    }
    Path("/results/phase-recommendation.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    asyncio.run(run())
