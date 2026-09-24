"""One serial subprocess per stage, with hard cancellation of its process group."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager, ExitStack, nullcontext
from typing import Literal

from scholight.logging.emf import emit_emf
from scholight.web_extract.admission import BoundedGate
from scholight.web_extract.cancellation import finish_after_cancellation
from scholight.web_extract.errors import ExtractError
from scholight.web_extract.process_family import kill_family, process_groups, reap_groups
from scholight.web_extract.reservations import RetainedMemoryPressureError


def _worker_error() -> ExtractError:
    return ExtractError(
        code="extract_worker_failed",
        message="Extraction worker exited unexpectedly.",
        status_code=503,
        retryable=True,
    )


class WorkerSupervisor:
    def __init__(
        self,
        kind: Literal["parser", "browser"],
        *,
        command: tuple[str, ...] | None = None,
        recycle_after: int = 100,
        queueing: bool = False,
        admit: Callable[[], None] | None = None,
        reserve_start: Callable[[], AbstractContextManager[None]] | None = None,
        recover_capacity: Callable[[], Awaitable[bool]] | None = None,
        capacity_recovered: Callable[[], None] | None = None,
    ) -> None:
        self.kind = kind
        self._command = command or (
            sys.executable,
            "-u",
            "-m",
            "scholight.web_extract.worker",
            kind,
        )
        self._process: asyncio.subprocess.Process | None = None
        self._gate = BoundedGate(
            "Parse" if kind == "parser" else "Browser",
            capacity=1,
            max_waiters=(4 if kind == "parser" else 2) if queueing else 0,
            wait_seconds=2 if kind == "parser" else 5,
        )
        self._admit = admit or (lambda: None)
        self._reserve_start = reserve_start or nullcontext
        self._recover_capacity = recover_capacity
        self._capacity_recovered = capacity_recovered or (lambda: None)
        self._stop_lock = asyncio.Lock()
        self._completed = 0
        self._recycle_after = recycle_after
        self.restarts = 0
        self._groups: set[int] = set()
        self._closing: asyncio.Task[None] | None = None
        self._warm_phases: set[str] = set()

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def busy(self) -> bool:
        return self._gate.active > 0

    def phase_warm(self, phase: Literal["pdf", "browser"]) -> bool:
        return (
            self._process is not None
            and self._process.returncode is None
            and phase in self._warm_phases
        )

    def mark_phase_warm(self, phase: Literal["pdf", "browser"]) -> None:
        if self._process is not None and self._process.returncode is None:
            self._warm_phases.add(phase)

    async def _start(self) -> None:
        with ExitStack() as startup:
            await self._start_reserved(startup)

    async def _start_reserved(self, startup: ExitStack) -> None:
        try:
            if self._process is not None and self._process.returncode is not None:
                # An idle crash can leave detached children holding the old pipes.
                # Reclaim that generation before discarding its group ownership.
                await self.close()
            async with self._stop_lock:
                if self._process is not None and self._process.returncode is None:
                    return
                # A cold generation adds native import/browser heaps beyond the
                # retained input. Keep this allowance until ready or reaped.
                startup.enter_context(self._reserve_start())
                spawning = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        *self._command,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        # Native diagnostics can include target content.
                        stderr=asyncio.subprocess.DEVNULL,
                        start_new_session=True,
                        limit=1024 * 1024,
                    )
                )
                try:
                    self._process = await asyncio.shield(spawning)
                except asyncio.CancelledError:
                    # Process creation can already have forked before returning its handle.
                    # Retain ownership before allowing cancellation to leave this scope.
                    self._process = await finish_after_cancellation(spawning)
                    self._groups = {self._process.pid}
                    raise
                self.restarts += 1
                self._warm_phases.clear()
                self._completed = 0
                self._groups = {self._process.pid}
            async with asyncio.timeout(15):
                if (await self._receive()).get("ready") is not True:
                    raise _worker_error()
            if self._process is not None:
                self._groups.update(process_groups(self._process.pid))
        except BaseException:
            await self.close()
            raise

    async def _receive(self) -> dict[str, object]:
        process = self._process
        if process is None or process.stdout is None:
            raise _worker_error()
        data = await process.stdout.readline()
        if not data:
            raise _worker_error()
        value = json.loads(data)
        if not isinstance(value, dict):
            raise _worker_error()
        return value

    async def warmup(self) -> None:
        async with self._gate.slot():
            await self._prepare(None)

    async def _prepare(
        self, message: dict[str, object] | Callable[[], dict[str, object]] | None
    ) -> dict[str, object] | None:
        for attempt in range(2):
            self._admit()
            try:
                try:
                    await self._start()
                except (OSError, ValueError) as error:
                    raise _worker_error() from error
                self._admit()
                prepared = message() if callable(message) else message
            except RetainedMemoryPressureError as error:
                if attempt or self._recover_capacity is None:
                    raise
                # No job has been dispatched. Prefer retiring the idle sibling;
                # retain this ready generation when that frees headroom.
                # Retain the caller's input lease and the FIFO execution permit.
                self._admit()
                try:
                    async with asyncio.timeout(2):
                        if not await self._recover_capacity():
                            await self.close()
                except asyncio.CancelledError:
                    await self.close()
                    raise
                except TimeoutError:
                    raise error from None
                except Exception as cleanup_error:
                    raise error from cleanup_error
                continue
            if attempt:
                self._capacity_recovered()
                emit_emf(service="extract", metrics={"MemoryPreparationRecovery": (1, "Count")})
            return prepared
        raise RuntimeError("Worker preparation exhausted its bounded attempts")

    async def call(
        self, message: dict[str, object] | Callable[[], dict[str, object]]
    ) -> dict[str, object]:
        async with self._gate.slot():
            # Startup has released its transient allowance. Its resident heap is
            # now in the fresh working-set sample used for job admission.
            prepared = await self._prepare(message)
            try:
                process = self._process
                if process is None or process.stdin is None:
                    raise _worker_error()
                process.stdin.write(json.dumps(prepared).encode() + b"\n")
                await process.stdin.drain()
                result = await self._receive()
                self._groups.update(process_groups(process.pid))
                self._completed += 1
                if self._completed >= self._recycle_after or result.get("retire") is True:
                    # Await death before a later request is allowed to start its successor.
                    await self.close()
                return result
            except asyncio.CancelledError:
                await self.close()
                raise
            except (OSError, ValueError, ExtractError) as error:
                await self.close()
                if isinstance(error, ExtractError):
                    raise
                raise _worker_error() from error

    async def close_if_idle(self) -> bool:
        if self.pid is None or self.busy or self._gate.waiting:
            return False
        # An uncontended acquire does not suspend; ownership is established
        # before close can yield and another call can start using the process.
        async with self._gate.slot():
            await self.close()
        return True

    async def close(self) -> None:
        if self._closing is None or self._closing.done():
            self._closing = asyncio.create_task(self._stop())
        try:
            await asyncio.shield(self._closing)
        except asyncio.CancelledError:
            await finish_after_cancellation(self._closing)
            raise

    async def _stop(self) -> None:
        async with self._stop_lock:
            process = self._process
            if process is None:
                return
            # Kill descendants even if the worker has already exited (e.g. Chromium).
            async with asyncio.timeout(2):
                groups = kill_family(process.pid, self._groups)
                await process.wait()
                while not reap_groups(groups):
                    await asyncio.sleep(0.01)
            self._process = None
