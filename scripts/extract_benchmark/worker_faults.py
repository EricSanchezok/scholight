"""Destructive process faults only inside an owned, isolated benchmark container."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
import urllib.request
from pathlib import Path

from checks import require

from scholight.web_extract.process_family import enable_subreaping, process_groups
from scholight.web_extract.spool import Spool
from scholight.web_extract.worker_supervisor import WorkerSupervisor


def alive_group(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    return True


def navigation_started(path: str) -> bool:
    # The only reachable endpoint is the runner-owned, isolated fixture server.
    with urllib.request.urlopen(  # nosec B310
        "http://93.184.216.2:8000/control/requests", timeout=1
    ) as response:
        return any(row["path"] == path for row in json.load(response))


async def run() -> None:
    if sys.platform != "linux":
        raise RuntimeError("Process fault evidence requires Linux")
    if os.environ.get("SCHOLIGHT_BENCHMARK_CONTAINER") != "1":
        raise RuntimeError("Explicit owned benchmark container marker is required")
    require(
        int(Path("/sys/fs/cgroup/memory.max").read_text()) == 768 * 1024 * 1024,
        "Fault container must retain the 768 MiB hard limit",
    )
    enable_subreaping()
    parser, browser = WorkerSupervisor("parser"), WorkerSupervisor("browser")
    spool = Spool(Path("/data/fault-spool"))
    spool.start()
    evidence = []
    try:
        await parser.warmup()
        old = parser.pid
        require(old is not None, "old is not None")
        groups = process_groups(old) | {old}
        os.kill(old, signal.SIGSTOP)
        started = time.monotonic()
        with spool.allocate(100) as body, spool.allocate(1000) as result:
            body.write(b"A real stopped parser must be killed.")
            try:
                async with asyncio.timeout(0.25):
                    await parser.call(
                        {
                            "request": {"url": "https://example.org", "render": "never"},
                            "body_path": str(body.path),
                            "result_path": str(result.path),
                            "result_limit": result.limit,
                            "fetched": {
                                "requested_url": "https://example.org",
                                "final_url": "https://example.org",
                                "status_code": 200,
                                "content_type": "text/plain",
                                "charset": "utf-8",
                            },
                        }
                    )
            except TimeoutError:
                pass
            else:
                raise AssertionError("Stopped parser escaped the hard deadline")
        duration = time.monotonic() - started
        require(duration <= 2.5 and parser.pid is None, "duration <= 2.5 and parser.pid is None")
        require(
            not any(alive_group(group) for group in groups),
            "not any(alive_group(group) for group in groups)",
        )
        require(spool.reserved_bytes == 0, "spool.reserved_bytes == 0")
        await parser.warmup()
        require(parser.pid != old, "parser.pid != old")
        evidence.append(
            {"fault": "stopped_parser", "deadline_and_cleanup_seconds": duration, "recovered": True}
        )

        await browser.warmup()
        old = browser.pid
        require(old is not None, "old is not None")
        groups = process_groups(old) | {old}
        os.kill(old, signal.SIGKILL)
        await asyncio.sleep(0.05)
        started = time.monotonic()
        await browser.warmup()
        require(browser.pid != old, "browser.pid != old")
        require(
            not any(alive_group(group) for group in groups),
            "not any(alive_group(group) for group in groups)",
        )
        evidence.append(
            {
                "fault": "idle_browser_worker_loss",
                "recovery_seconds": time.monotonic() - started,
                "old_groups_reaped": True,
            }
        )

        current = browser.pid
        require(current is not None, "current is not None")
        descendants = process_groups(current) - {current}
        require(descendants, "descendants")
        with spool.allocate(1000) as result:
            target = "/fault/slow?active-chromium-loss"
            pending = asyncio.create_task(
                browser.call(
                    {
                        "request": {
                            "url": "http://93.184.216.2:8000" + target,
                            "render": "always",
                        },
                        "result_path": str(result.path),
                        "result_limit": result.limit,
                    }
                )
            )
            try:
                async with asyncio.timeout(3):
                    while not await asyncio.to_thread(navigation_started, target):
                        require(not pending.done(), "Browser failed before navigation was observed")
                        await asyncio.sleep(0.02)
                require(not pending.done(), "Navigation must be active when Chromium is killed")
                started = time.monotonic()
                for group in descendants:
                    os.killpg(group, signal.SIGKILL)
                async with asyncio.timeout(3):
                    reply = await pending
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            require(
                "error" in reply and reply.get("retire") is True,
                '"error" in reply and reply.get("retire") is True',
            )
            require(browser.pid is None, "browser.pid is None")
        require(
            not any(alive_group(group) for group in descendants | {current}),
            "not any(alive_group(group) for group in descendants | {current})",
        )
        evidence.append(
            {
                "fault": "chromium_connection_loss",
                "failure_and_cleanup_seconds": time.monotonic() - started,
                "retired": True,
                "navigation_observed_before_kill": True,
            }
        )

        await browser.warmup()
        current = browser.pid
        require(current is not None, "current is not None")
        groups = process_groups(current) | {current}
        await asyncio.gather(*(browser.close() for _ in range(8)))
        require(
            not any(alive_group(group) for group in groups),
            "not any(alive_group(group) for group in groups)",
        )
        evidence.append({"fault": "concurrent_close", "callers": 8, "old_groups_reaped": True})
        print(json.dumps({"passed": True, "evidence": evidence}), flush=True)
    finally:
        await asyncio.gather(parser.close(), browser.close())
        spool.close()


if __name__ == "__main__":
    asyncio.run(run())
