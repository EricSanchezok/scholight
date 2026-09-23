"""One serial subprocess per stage, with hard cancellation of its process group."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Literal

from scholight.web_extract.errors import ExtractError
from scholight.web_extract.process_family import kill_family, process_groups, reap_groups


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
        self._lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._completed = 0
        self._recycle_after = recycle_after
        self.restarts = 0
        self._groups: set[int] = set()

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def _start(self) -> None:
        try:
            async with self._stop_lock:
                if self._process is not None and self._process.returncode is None:
                    return
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
                    self._process = await asyncio.shield(spawning)
                    self._groups = {self._process.pid}
                    raise
                self.restarts += 1
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
        async with self._lock:
            await self._start()

    async def call(self, message: dict[str, object]) -> dict[str, object]:
        if self._lock.locked():
            raise ExtractError(
                code="extract_capacity_exceeded",
                message="Extraction worker is busy.",
                status_code=503,
                retryable=True,
            )
        async with self._lock:
            try:
                await self._start()
                process = self._process
                if process is None or process.stdin is None:
                    raise _worker_error()
                process.stdin.write(json.dumps(message).encode() + b"\n")
                await process.stdin.drain()
                result = await self._receive()
                self._groups.update(process_groups(process.pid))
                self._completed += 1
                if self._completed >= self._recycle_after:
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

    async def close(self) -> None:
        async with self._stop_lock:
            process = self._process
            if process is None:
                return
            # Kill descendants even if the worker has already exited (e.g. Chromium).
            async with asyncio.timeout(2):
                groups = kill_family(process.pid, self._groups)
                await asyncio.shield(process.wait())
                while not reap_groups(groups):
                    await asyncio.sleep(0.01)
            self._process = None
