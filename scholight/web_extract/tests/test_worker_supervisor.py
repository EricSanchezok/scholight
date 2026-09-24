from __future__ import annotations

import asyncio
import os
import sys
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest

from scholight.web_extract.admission import capacity_error
from scholight.web_extract.errors import ExtractError
from scholight.web_extract.reservations import (
    MemoryBudget,
    MemoryModel,
    RetainedMemoryPressureError,
    StageCost,
)
from scholight.web_extract.worker_supervisor import WorkerSupervisor

pytestmark = pytest.mark.asyncio

ECHO = """
import json, os, sys, time
import subprocess
print(json.dumps({'ready': True}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    if message.get('retire'):
        print(json.dumps({'pid': os.getpid(), 'retire': True}), flush=True)
        continue
    if message.get('child'):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        print(json.dumps({'pid': child.pid}), flush=True)
        continue
    if message.get('hang'):
        time.sleep(60)
    print(json.dumps({'pid': os.getpid()}), flush=True)
"""


async def test_retained_heap_pressure_reclaims_before_dispatch_and_keeps_input_lease() -> None:
    working = 150
    budget = MemoryBudget(
        lambda: working,
        lambda: None,
        model=MemoryModel(download=StageCost(10, 0), html=StageCost(80, 0)),
        high=200,
    )
    lease = budget.lease()
    lease.transfer("download")
    attempts = []

    async def recover():
        nonlocal working
        assert budget.reserved_bytes == 10
        working = 100

    def prepare():
        attempts.append(worker.pid)
        lease.transfer("parse", size=1, mime="text/html")
        return {}

    worker = WorkerSupervisor(
        "parser", command=(sys.executable, "-u", "-c", ECHO), recover_capacity=recover
    )
    try:
        result = await worker.call(prepare)
        assert len(attempts) == 2 and attempts[0] != result["pid"] == attempts[1]
        assert budget.reserved_bytes == 80
        with pytest.raises(ProcessLookupError):
            os.kill(attempts[0], 0)
    finally:
        await worker.close()
        lease.close()


async def test_pre_dispatch_memory_recovery_is_bounded_to_one_attempt() -> None:
    budget = MemoryBudget(
        lambda: 150, lambda: None, model=MemoryModel(html=StageCost(80, 0)), high=200
    )
    lease = budget.lease()
    recover = AsyncMock()
    worker = WorkerSupervisor(
        "parser", command=(sys.executable, "-u", "-c", ECHO), recover_capacity=recover
    )

    def prepare():
        lease.transfer("parse", size=1, mime="text/html")
        return {}

    try:
        with pytest.raises(ExtractError):
            await worker.call(prepare)
        assert worker.restarts == 2
        recover.assert_awaited_once()
    finally:
        await worker.close()
        lease.close()


async def test_cold_start_admission_can_reclaim_before_spawning() -> None:
    working = 150
    budget = MemoryBudget(
        lambda: working, lambda: None, model=MemoryModel(parser_startup=80), high=200
    )

    async def recover():
        nonlocal working
        assert worker.pid is None and budget.reserved_bytes == 0
        working = 100

    worker = WorkerSupervisor(
        "parser",
        command=(sys.executable, "-u", "-c", ECHO),
        reserve_start=lambda: budget.startup("parser"),
        recover_capacity=recover,
    )
    try:
        await worker.warmup()
        assert worker.restarts == 1 and budget.reserved_bytes == 0
    finally:
        await worker.close()


async def test_cancellation_during_preparation_recovery_owns_worker_cleanup() -> None:
    recovering = asyncio.Event()

    async def recover():
        recovering.set()
        await asyncio.Event().wait()

    def prepare():
        raise RetainedMemoryPressureError

    worker = WorkerSupervisor(
        "parser", command=(sys.executable, "-u", "-c", ECHO), recover_capacity=recover
    )
    task = asyncio.create_task(worker.call(prepare))
    try:
        await asyncio.wait_for(recovering.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert worker.pid is None and not worker.busy
    finally:
        await worker.close()


async def test_memory_failure_after_dispatch_never_replays_worker_execution() -> None:
    recover = AsyncMock()
    worker = WorkerSupervisor(
        "browser", command=(sys.executable, "-u", "-c", ECHO), recover_capacity=recover
    )
    try:
        await worker.warmup()
        with patch.object(worker, "_receive", side_effect=RetainedMemoryPressureError()):
            with pytest.raises(ExtractError):
                await worker.call({})
        recover.assert_not_awaited()
        assert worker.restarts == 1 and worker.pid is None
    finally:
        await worker.close()


async def test_queue_or_competing_capacity_error_does_not_recycle_workers() -> None:
    recover = AsyncMock()
    worker = WorkerSupervisor(
        "parser", command=(sys.executable, "-u", "-c", ECHO), recover_capacity=recover
    )

    def prepare():
        raise capacity_error()

    try:
        with pytest.raises(ExtractError):
            await worker.call(prepare)
        recover.assert_not_awaited()
        assert worker.restarts == 1
    finally:
        await worker.close()


async def test_pressure_reclamation_does_not_interrupt_an_active_sibling() -> None:
    worker = WorkerSupervisor("browser", command=(sys.executable, "-u", "-c", ECHO))
    await worker.warmup()
    task = asyncio.create_task(worker.call({"hang": True}))
    try:
        await asyncio.sleep(0.03)
        assert await worker.close_if_idle() is False
        assert not task.done() and worker.pid is not None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await worker.close()


async def test_pressure_reclamation_owns_the_idle_worker_gate_until_reaped() -> None:
    worker = WorkerSupervisor("browser", command=(sys.executable, "-u", "-c", ECHO))
    await worker.warmup()
    process = worker.pid
    try:
        assert await worker.close_if_idle() is True
        assert worker.pid is None and not worker.busy
        with pytest.raises(ProcessLookupError):
            os.kill(process, 0)
    finally:
        await worker.close()


async def test_cold_start_is_reserved_before_spawn_and_warm_calls_do_not_reserve() -> None:
    working = 100
    budget = MemoryBudget(
        lambda: working, lambda: None, model=MemoryModel(parser_startup=80), high=200
    )
    worker = WorkerSupervisor(
        "parser",
        command=(sys.executable, "-u", "-c", ECHO),
        reserve_start=lambda: budget.startup("parser"),
        recycle_after=2,
    )
    receive = worker._receive
    measured = []

    async def measured_receive():
        measured.append(budget.reserved_bytes)
        return await receive()

    try:
        with patch.object(worker, "_receive", measured_receive):
            await worker.warmup()
            assert measured == [80] and budget.reserved_bytes == 0
            working = 130
            first = await worker.call({})
            assert (await worker.call({})) == first
            assert measured == [80, 0, 0] and worker.pid is None
            with pytest.raises(ExtractError):
                await worker.call({})
        assert worker.restarts == 1 and budget.reserved_bytes == 0
    finally:
        await worker.close()


async def test_cold_start_peak_is_replaced_by_resident_memory_before_job_admission() -> None:
    working = 100
    budget = MemoryBudget(
        lambda: working,
        lambda: None,
        model=MemoryModel(parser_startup=80, html=StageCost(50, 0)),
        high=200,
    )
    lease = budget.lease()

    @contextmanager
    def startup():
        nonlocal working
        with budget.startup("parser"):
            yield
            working = 150  # Native startup has settled into a smaller resident heap.

    def prepare():
        lease.transfer("parse", size=10, mime="text/html")
        return {}

    worker = WorkerSupervisor(
        "parser", command=(sys.executable, "-u", "-c", ECHO), reserve_start=startup
    )
    try:
        assert (await worker.call(prepare))["pid"] == worker.pid
        assert working == 150 and budget.reserved_bytes == 50
    finally:
        await worker.close()
        lease.close()


async def test_idle_crash_replacement_also_requires_startup_memory() -> None:
    working = 100
    budget = MemoryBudget(
        lambda: working, lambda: None, model=MemoryModel(parser_startup=80), high=200
    )
    worker = WorkerSupervisor(
        "parser",
        command=(sys.executable, "-u", "-c", ECHO),
        reserve_start=lambda: budget.startup("parser"),
    )
    try:
        await worker.warmup()
        process = worker._process
        assert process is not None
        process.kill()
        await process.wait()
        working = 130
        with pytest.raises(ExtractError):
            await worker.call({})
        assert worker.restarts == 1 and worker.pid is None and budget.reserved_bytes == 0
    finally:
        await worker.close()


async def test_cancelled_readiness_releases_startup_only_after_process_cleanup() -> None:
    budget = MemoryBudget(lambda: 100, lambda: None, model=MemoryModel(parser_startup=80), high=200)
    worker = WorkerSupervisor(
        "parser",
        command=(sys.executable, "-u", "-c", ECHO),
        reserve_start=lambda: budget.startup("parser"),
    )
    receiving = asyncio.Event()

    async def delayed_ready():
        receiving.set()
        await asyncio.Event().wait()

    try:
        with patch.object(worker, "_receive", delayed_ready):
            task = asyncio.create_task(worker.warmup())
            await asyncio.wait_for(receiving.wait(), timeout=2)
            assert budget.reserved_bytes == 80
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert worker.pid is None and budget.reserved_bytes == 0
    finally:
        await worker.close()


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


async def test_worker_can_retire_after_returning_a_result() -> None:
    worker = WorkerSupervisor("browser", command=(sys.executable, "-u", "-c", ECHO))
    try:
        result = await worker.call({"retire": True})
        assert worker.pid is None
        with pytest.raises(ProcessLookupError):
            os.kill(result["pid"], 0)
        assert (await worker.call({}))["pid"] != result["pid"]
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


async def test_replacing_an_idle_crashed_worker_reclaims_its_descendants() -> None:
    worker = WorkerSupervisor("browser", command=(sys.executable, "-u", "-c", ECHO))
    child = await worker.call({"child": True})
    process = worker._process
    assert process is not None
    process.kill()
    # The orphan still inherits the pipe, so wait() can wait for its EOF too.
    async with asyncio.timeout(1):
        while process.returncode is None:
            await asyncio.sleep(0.01)
    try:
        await worker.call({})
        import subprocess

        status = subprocess.run(
            ["/bin/ps", "-o", "stat=", "-p", str(child["pid"])],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        assert not status or status.startswith("Z")
    finally:
        await worker.close()
        try:
            os.kill(child["pid"], 9)
        except ProcessLookupError:
            pass


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


async def test_repeated_cancellation_still_waits_for_spawn_ownership() -> None:
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
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert process.returncode is not None
    finally:
        await worker.close()
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


async def test_repeated_cancellation_waits_for_owned_cleanup() -> None:
    worker = WorkerSupervisor("parser", command=(sys.executable, "-u", "-c", ECHO))
    await worker.warmup()
    process = worker._process
    assert process is not None
    wait = process.wait
    waiting = asyncio.Event()

    async def delayed_wait():
        waiting.set()
        await asyncio.sleep(0.05)
        return await wait()

    try:
        with patch.object(process, "wait", delayed_wait):
            closing = asyncio.create_task(worker.close())
            await waiting.wait()
            closing.cancel()
            await asyncio.sleep(0)
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
        assert worker.pid is None
    finally:
        await worker.close()
