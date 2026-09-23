"""Exclusive, bounded scratch files owned by one Extract supervisor."""

from __future__ import annotations

import fcntl
import os
import secrets
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

from scholight.web_extract.errors import ExtractError


class SpoolFile:
    def __init__(self, spool: Spool, path: Path, limit: int) -> None:
        self._spool = spool
        self.path = path
        self.limit = limit
        self.size = 0
        self._closed = False

    def write(self, data: bytes) -> None:
        if self.size + len(data) > self.limit:
            raise ExtractError(
                code="response_too_large",
                message="Document exceeds the scratch file limit.",
                status_code=413,
                retryable=False,
            )
        with self.path.open("ab") as stream:
            stream.write(data)
        self.size += len(data)

    def close(self) -> None:
        if not self._closed:
            self.path.unlink(missing_ok=True)
            self._spool.release(self)
            self._closed = True

    def __enter__(self) -> SpoolFile:
        return self

    def __exit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()


class Spool:
    def __init__(self, root: Path, *, max_bytes: int = 256 * 1024 * 1024) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.reserved_bytes = 0
        self._lock: BinaryIO | None = None
        self._files: set[SpoolFile] = set()

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = (self.root / ".owner.lock").open("a+b")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            lock.close()
            raise
        self._lock = lock
        for path in self.root.glob("extract-*.tmp"):
            path.unlink(missing_ok=True)

    def allocate(self, limit: int) -> SpoolFile:
        if self._lock is None:
            raise RuntimeError("Extract scratch storage has not started")
        if limit < 0 or self.reserved_bytes + limit > self.max_bytes:
            raise ExtractError(
                code="extract_capacity_exceeded",
                message="Extract scratch capacity is exhausted.",
                status_code=503,
                retryable=True,
            )
        path = self.root / f"extract-{secrets.token_hex(16)}.tmp"
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        result = SpoolFile(self, path, limit)
        self._files.add(result)
        self.reserved_bytes += limit
        return result

    def release(self, body: SpoolFile) -> None:
        self._files.remove(body)
        self.reserved_bytes -= body.limit

    def close(self) -> None:
        for body in list(self._files):
            body.close()
        if self._lock is not None:
            self._lock.close()
            self._lock = None
