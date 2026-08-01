"""Bounded, cancellable RCM subprocess primitives for Survey workers."""

from __future__ import annotations

import asyncio
import os
import re
import signal
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field

from scholight.survey.contracts import STDERR_TAIL_MAX_BYTES

_TOKEN_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret)\s*[:=]\s*)[^\s]+"),
    re.compile(r"sk_live_[A-Za-z0-9._~-]+"),
)
_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._~-])/(?:data|tmp|var|home|Users|proc|etc)(?:/[^\s:'\"]+)+"
)


class ProcessOutputTooLargeError(Exception):
    """A child emitted more output than the worker may retain."""


@dataclass(slots=True)
class ProcessControl:
    """Share one process-group cancellation boundary with its heartbeat."""

    lease_lost: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_requested: asyncio.Event = field(default_factory=asyncio.Event)
    process: asyncio.subprocess.Process | None = None

    async def attach(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        if self.lease_lost.is_set() or self.cancel_requested.is_set():
            await terminate_process_group(process)

    async def lose_lease(self) -> None:
        self.lease_lost.set()
        if self.process is not None:
            await terminate_process_group(self.process)

    async def request_cancel(self) -> None:
        """Stop this task without conflating a user cancellation with lease loss."""
        self.cancel_requested.set()
        if self.process is not None:
            await terminate_process_group(self.process)


async def terminate_process_group(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float = 10,
) -> None:
    """Terminate the entire RCM process group and reap its leader."""
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return
    except TimeoutError:
        pass
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


async def write_stdin(process: asyncio.subprocess.Process, value: str) -> None:
    if process.stdin is None:
        raise RuntimeError("RCM stdin is unavailable")
    process.stdin.write(value.encode("utf-8"))
    await process.stdin.drain()
    process.stdin.close()
    await process.stdin.wait_closed()


async def read_bounded(
    stream: asyncio.StreamReader | None,
    *,
    limit: int,
) -> bytes:
    if stream is None:
        raise RuntimeError("RCM output stream is unavailable")
    chunks: list[bytes] = []
    size = 0
    while chunk := await stream.read(min(64 * 1024, limit + 1 - size)):
        size += len(chunk)
        if size > limit:
            raise ProcessOutputTooLargeError
        chunks.append(chunk)
    return b"".join(chunks)


async def read_sanitized_tail(
    stream: asyncio.StreamReader | None,
    *,
    limit: int = STDERR_TAIL_MAX_BYTES,
) -> str:
    if stream is None:
        return ""
    chunks: deque[bytes] = deque()
    size = 0
    while chunk := await stream.read(16 * 1024):
        chunks.append(chunk)
        size += len(chunk)
        while chunks and size - len(chunks[0]) >= limit:
            size -= len(chunks.popleft())
    raw = b"".join(chunks)[-limit:].decode("utf-8", errors="replace")
    sanitized = raw.replace("/data/surveys/", "<survey-workspace>/")
    for pattern in _TOKEN_PATTERNS:
        sanitized = pattern.sub(r"\1<redacted>" if pattern.groups else "<redacted>", sanitized)
    return _PATH_PATTERN.sub("<redacted-path>", sanitized)


def classify_rcm_error(stderr_tail: str) -> tuple[str, str]:
    """Map diagnostic text to stable non-sensitive public semantics."""
    text = stderr_tail.casefold()
    if any(marker in text for marker in ("unauthorized", "invalid api key", "status 401")):
        return "survey_model_auth_failed", "The Survey model could not be authenticated."
    if any(marker in text for marker in ("rate limit", "too many requests", "status 429")):
        return "survey_provider_rate_limited", "A Survey provider is temporarily rate limited."
    if "survey_quota_exceeded" in text or "search quota" in text:
        return "survey_search_quota_exceeded", "The Survey search allowance was reached."
    if "mcp" in text and any(marker in text for marker in ("connect", "unavailable", "timeout")):
        return "survey_mcp_unavailable", "Scholight paper search is temporarily unavailable."
    if any(marker in text for marker in ("sandbox", "outside run", "path traversal")):
        return "survey_sandbox_violation", "The Survey workflow attempted an unsafe file access."
    if any(marker in text for marker in ("out of memory", "cannot allocate memory", "signal: 9")):
        return "survey_resource_exhausted", "The Survey worker exhausted its resources."
    if "tool" in text:
        return "survey_tool_failed", "A Survey research tool did not complete successfully."
    return "survey_runtime_unavailable", "The Survey runtime did not complete successfully."


__all__ = [
    "ProcessControl",
    "ProcessOutputTooLargeError",
    "classify_rcm_error",
    "read_bounded",
    "read_sanitized_tail",
    "terminate_process_group",
    "write_stdin",
]
