"""Per-operation budgets and redacted extraction diagnostics, never target secrets."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class ExtractTrace:
    request_id: str
    deadline: float
    phases: dict[str, float] = field(default_factory=dict)
    download_bytes: int = 0
    source_bytes: int = 0
    dom_bytes: int = 0
    upstream_status: int | None = None
    mime: str = "unknown"
    cache_key_id: str | None = None
    cache_entry_bytes: int = 0
    cache_ttl_seconds: int = 0
    started: float = field(default_factory=time.perf_counter)

    def remaining(self) -> float:
        return max(0, self.deadline - time.monotonic())


current_trace: ContextVar[ExtractTrace | None] = ContextVar("extract_trace", default=None)


def safe_request_id(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        return value
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def mime_category(content_type: str) -> str:
    mime = content_type.partition(";")[0].strip().lower()
    if mime in {"text/html", "application/xhtml+xml"}:
        return "html"
    if mime == "application/pdf":
        return "pdf"
    if mime in {"application/json", "text/json"}:
        return "json"
    if mime in {"application/xml", "text/xml"}:
        return "xml"
    return "text" if mime.startswith("text/") else "other"


@contextmanager
def phase(name: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        if (trace := current_trace.get()) is not None:
            trace.phases[name] = trace.phases.get(name, 0) + (time.perf_counter() - started) * 1000


def log_completion(
    *,
    trace: ExtractTrace,
    scope: str,
    outcome: str,
    render: str,
    cache_eligible: bool,
    cache_hit: bool,
    pagination: bool = False,
) -> None:
    try:
        _log_completion(trace, scope, outcome, render, cache_eligible, cache_hit, pagination)
    except Exception:
        # A broken log sink must not replace a result/error or prevent context cleanup.
        # Logging this failure through the same sink could recurse.
        return


def _log_completion(
    trace: ExtractTrace,
    scope: str,
    outcome: str,
    render: str,
    cache_eligible: bool,
    cache_hit: bool,
    pagination: bool,
) -> None:
    logger.info(
        "extract_completed",
        request_id=trace.request_id,
        scope=scope,
        outcome=outcome,
        render_mode=render,
        mime=trace.mime,
        pagination=pagination,
        cache_eligible=cache_eligible,
        cache_hit=cache_hit,
        cache_key_id=trace.cache_key_id,
        cache_entry_bytes=trace.cache_entry_bytes,
        cache_ttl_seconds=trace.cache_ttl_seconds,
        duration_ms=(time.perf_counter() - trace.started) * 1000,
        static_download_bytes=trace.download_bytes,
        source_document_bytes=trace.source_bytes,
        rendered_dom_bytes=trace.dom_bytes,
        upstream_status=trace.upstream_status,
        phases_ms=trace.phases,
    )
