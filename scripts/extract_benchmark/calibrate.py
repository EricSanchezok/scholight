"""Measure native parser phase envelopes; keep all workers in the 768 MiB cgroup."""

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

from corpus import Case, _html, _pdf, corpus

from scholight.models.web_extract import ExtractResponseFormat, RenderMode

# Load the production supervisor's dependency graph in the measured parent.
from scholight.web_extract import runtime  # noqa: F401
from scholight.web_extract.engine import ExtractInput, FetchResult
from scholight.web_extract.isolated import IsolatedParser
from scholight.web_extract.memory import read_cgroup
from scholight.web_extract.process_family import enable_subreaping
from scholight.web_extract.spool import Spool
from scholight.web_extract.worker_supervisor import WorkerSupervisor

MIB = 1024 * 1024


def calibration_cases() -> list[Case]:
    cases = [c for c in corpus() if c.category != "javascript" and c.status == 200]
    for size in (8192, 131_072, 524_288, 1_048_576):
        for kind, fragment in [
            ("prose", "<p>A complete sentence preserves research evidence and limitations.</p>"),
            ("dense", "<p>Evidence.</p>"),
            ("table", "<tr><td>Model</td><td>0.915</td></tr>"),
            ("chinese", "<p>中文证据应当被完整保留。</p>"),
        ]:
            repeated = fragment * max(1, size // len(fragment.encode()))
            if kind == "table":
                repeated = "<table>" + repeated + "</table>"
            cases.append(Case(f"{kind}-{size}", "html", "text/html", _html(kind, repeated), ()))
        cases.append(Case(f"text-{size}", "text", "text/plain", b"evidence " * (size // 9), ()))
    for size in (1000, 10_000, 100_000):
        cases.append(
            Case(
                f"pdf-stream-{size}", "pdf", "application/pdf", _pdf("Evidence " * (size // 9)), ()
            )
        )
    return cases


def recommended(rows: list[dict]) -> dict:
    if not rows or any(not row["completed"] for row in rows):
        raise ValueError("Cannot recommend a memory envelope from incomplete calibration")
    if not {"parser_startup", "browser_startup"} <= {r["kind"] for r in rows}:
        raise ValueError("Missing cold worker startup measurements")
    result = {}
    for kind in ("parser_startup", "browser_startup"):
        samples = [r for r in rows if r["kind"] == kind]
        result[kind] = {
            "fixed_bytes": math.ceil((1.5 * max(r["peak_delta"] for r in samples) + 8 * MIB) / MIB)
            * MIB,
            "samples": len(samples),
        }
    for kind in ("html", "pdf", "text"):
        samples = [r for r in rows if r["kind"] == kind and r["completed"]]
        small = [r for r in samples if r["input_bytes"] <= 65_536]
        fixed = (
            math.ceil((1.5 * max((r["peak_delta"] for r in small), default=0) + 8 * MIB) / MIB)
            * MIB
        )
        multiplier = max(
            1,
            math.ceil(
                max((1.5 * r["peak_delta"] - fixed) / max(1, r["input_bytes"]) for r in samples)
            ),
        )
        result[kind] = {"fixed_bytes": fixed, "per_input_byte": multiplier, "samples": len(samples)}
    return result


async def startup_sample(worker: WorkerSupervisor, trial: int) -> dict:
    await worker.close()
    gc.collect()
    baseline = peak = read_cgroup().working_set
    started = time.monotonic()
    task = asyncio.create_task(worker.warmup())
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
        return {
            "case": f"{worker.kind}-cold-start",
            "round": trial,
            "kind": f"{worker.kind}_startup",
            "input_bytes": 0,
            "baseline": baseline,
            "peak": peak,
            "peak_delta": max(0, peak - baseline),
            "stopped_by_probe": stopped,
            "completed": not isinstance(result[0], BaseException),
            "error_type": type(result[0]).__name__
            if isinstance(result[0], BaseException)
            else None,
            "elapsed_seconds": time.monotonic() - started,
        }
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def run() -> None:
    if (
        sys.platform != "linux"
        or platform.machine() not in {"arm64", "aarch64"}
        or sys.version_info[:2] != (3, 11)
    ):
        raise RuntimeError("Calibration requires Linux ARM64 / Python 3.11")
    if (
        os.environ.get("SCHOLIGHT_BENCHMARK_CONTAINER") != "1"
        or int(Path("/sys/fs/cgroup/memory.max").read_text()) != 768 * MIB
    ):
        raise RuntimeError("Calibration requires an owned container with a 768 MiB hard limit")
    output = Path("/results/calibration")
    output.mkdir(parents=True, exist_ok=False)
    enable_subreaping()
    spool = Spool(Path("/data/calibration-spool"))
    spool.start()
    browser = WorkerSupervisor("browser")
    parser = WorkerSupervisor("parser")
    adapter = IsolatedParser(parser, spool, max_output_bytes=50_000_000)
    rows = []
    try:
        await parser.warmup()
        await browser.warmup()
        with (output / "measurements.jsonl").open("w") as records:
            for trial in range(5):
                for worker in (parser, browser):
                    row = await startup_sample(worker, trial)
                    rows.append(row)
                    records.write(json.dumps(row) + "\n")
                    records.flush()
                    if row["stopped_by_probe"] or not row["completed"]:
                        raise RuntimeError(
                            "Cold startup calibration failed; inspect partial evidence"
                        )
            for trial in range(5):
                for case in calibration_cases():
                    await parser.close()
                    await parser.warmup()
                    gc.collect()
                    with spool.allocate(len(case.body)) as body:
                        body.write(case.body)
                        fetched = FetchResult(
                            "https://example.org",
                            "https://example.org",
                            200,
                            case.mime,
                            "utf-8",
                            spool_file=body,
                        )
                        baseline = read_cgroup().working_set
                        peak = baseline
                        started = time.monotonic()
                        task = asyncio.create_task(
                            adapter.parse(
                                fetched,
                                ExtractInput(
                                    "https://example.org",
                                    RenderMode.NEVER,
                                    ExtractResponseFormat.MAIN_MARKDOWN,
                                ),
                                rendered=False,
                            )
                        )
                        stopped = False
                        while not task.done():
                            peak = max(peak, read_cgroup().working_set)
                            if peak >= 640 * MIB or time.monotonic() - started > 45:
                                stopped = True
                                task.cancel()
                                break
                            await asyncio.sleep(0.01)
                        result = await asyncio.gather(task, return_exceptions=True)
                        peak = max(peak, read_cgroup().working_set)
                        kind = (
                            "pdf"
                            if case.mime == "application/pdf"
                            else "html"
                            if case.mime == "text/html"
                            else "text"
                        )
                        row = {
                            "case": case.name,
                            "round": trial,
                            "kind": kind,
                            "input_bytes": len(case.body),
                            "baseline": baseline,
                            "peak": peak,
                            "peak_delta": max(0, peak - baseline),
                            "stopped_by_probe": stopped,
                            "completed": not isinstance(result[0], BaseException),
                            "error_type": type(result[0]).__name__
                            if isinstance(result[0], BaseException)
                            else None,
                            "elapsed_seconds": time.monotonic() - started,
                        }
                        rows.append(row)
                        records.write(json.dumps(row) + "\n")
                        records.flush()
                        del result, task, fetched
                        if stopped or not row["completed"]:
                            raise RuntimeError(
                                "Calibration failed or hit its guard; inspect partial evidence"
                            )
    finally:
        await asyncio.gather(parser.close(), browser.close())
        spool.close()
    report = {
        "python": sys.version,
        "machine": platform.machine(),
        "rounds": 5,
        "sample_interval_seconds": 0.01,
        "recommendation": recommended(rows),
        "limitations": [
            "Observed envelope with 50% margin plus 8 MiB; not a bound on arbitrary compressed documents.",
            "Parser-only calibration with browser idle. Validate combined workloads separately.",
            "Cold startup envelopes cover native imports and browser warmup with a resident sibling.",
            "Download/browser envelopes require end-to-end phase measurements.",
        ],
    }
    (output / "recommendation.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    asyncio.run(run())
