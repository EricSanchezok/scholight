from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import patch

import pytest

from scholight.web_extract.errors import ExtractError
from scholight.web_extract.worker_supervisor import WorkerSupervisor

pytestmark = pytest.mark.asyncio

ECHO = """
import json, os, sys, time
import subprocess
print(json.dumps({'ready': True}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    if message.get('child'):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        print(json.dumps({'pid': child.pid}), flush=True)
        continue
    if message.get('hang'):
        time.sleep(60)
    print(json.dumps({'pid': os.getpid()}), flush=True)
"""


async def test_worker_is_reused_then_recycled_without_overlap() -> None:
    worker = WorkerSupervisor("parser", command=(sys.executable, "-u", "-c", ECHO), recycle_after=2)
    try:
        first = await worker.call({})
        second = await worker.call({})
        with pytest.raises(ProcessLookupError):
            os.kill(first["pid"], 0)
        third = await worker.call({})
        assert first == second and third != first
    finally:
        await worker.close()


async def test_cancelling_a_hung_worker_kills_execution_and_allows_replacement() -> None:
    worker = WorkerSupervisor("parser", command=(sys.executable, "-u", "-c", ECHO))
    await worker.warmup()
    pid = worker.pid
    task = asyncio.create_task(worker.call({"hang": True}))
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    try:
        assert (await worker.call({}))["pid"] != pid
    finally:
        await worker.close()


async def test_worker_crash_is_a_controlled_retryable_error() -> None:
    command = (sys.executable, "-u", "-c", "print('{\"ready\": true}', flush=True)")
    worker = WorkerSupervisor("parser", command=command)
    try:
        with pytest.raises(ExtractError, match="worker exited"):
            await worker.call({})
    finally:
        await worker.close()


async def test_worker_close_kills_detached_descendant_groups() -> None:
    worker = WorkerSupervisor("browser", command=(sys.executable, "-u", "-c", ECHO))
    child = await worker.call({"child": True})
    await worker.close()
    # The OS may briefly retain an orphan zombie; it must not execute any work.
    import subprocess

    status = subprocess.run(
        ["/bin/ps", "-o", "stat=", "-p", str(child["pid"])],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    assert not status or status.startswith("Z")


async def test_cancellation_during_spawn_cannot_orphan_the_new_process() -> None:
    spawn = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    process = None

    async def delayed_spawn(*args, **kwargs):
        nonlocal process
        process = await spawn(*args, **kwargs)
        spawned.set()
        await asyncio.sleep(0.05)
        return process

    worker = WorkerSupervisor("parser", command=(sys.executable, "-u", "-c", ECHO))
    try:
        with patch("asyncio.create_subprocess_exec", delayed_spawn):
            task = asyncio.create_task(worker.call({}))
            await spawned.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert process.returncode is not None
    finally:
        await worker.close()
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
