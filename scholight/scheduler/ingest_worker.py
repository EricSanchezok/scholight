"""Single-paper worker for the durable ingestion queue."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import os
import re
import shutil
import signal
import socket
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Literal, ParamSpec, TypeVar

import structlog

from scholight.config import require_full_runtime, settings
from scholight.db.fulltext_install import target_install
from scholight.db.ingestion import (
    IngestionJob,
    claim_ingestion_job,
    complete_ingestion_job,
    configured_queue,
    enqueue_ingestion_job,
    fail_ingestion_job,
    get_ingestion_status,
    release_ingestion_job,
    renew_ingestion_job_lease,
    verified_sync_source,
)
from scholight.db.target_ingestion import TargetLeaseLostError
from scholight.logging.emf import emit_emf
from scholight.models.ingestion_target import fulltext_profile_hash
from scholight.pipeline.chunkers.md_chunker import chunk_markdown
from scholight.pipeline.embedder import Embedder
from scholight.pipeline.latex_md import LatexMdError, LatexResourceLimitError, latex_to_markdown
from scholight.pipeline.pdf_md import PDFMdError, pdf_to_markdown
from scholight.scheduler.resources import (
    DownloadedResource,
    ResourceCorruptError,
    ResourceTemporaryError,
    ResourceUnavailableError,
    fetch_paper_resource,
    fetch_pdf_resource,
)
from scholight.sources.arxiv import canonicalize_arxiv_id
from scholight.store.fulltext_install import settled_io
from scholight.store.ingestion import (
    IngestionSafetyError,
    get_paper,
    install_paper_chunks,
)
from scholight.utils.text import truncate_utf8

logger = structlog.get_logger(__name__)
_SCRATCH_ROOT = Path("/data/ingestion")
_POLL_SECONDS = 10
_ERROR_SECRET = re.compile(
    r"(?i)((?:token|key|password|authorization)[\"']?\s*[=:]\s*)"
    r"(?:Bearer\s+)?(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&]+)"
)


P = ParamSpec("P")
T = TypeVar("T")


async def _work_io(operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    return await settled_io(partial(operation, *args, **kwargs))


async def _target_obsolete(job: IngestionJob, version: int) -> str:
    queue = configured_queue()
    if queue is not None:
        await queue.record_scope(
            [(job.arxiv_id, version)], dt.datetime.now(dt.UTC).date(), "revision"
        )
        await enqueue_ingestion_job(
            job.arxiv_id, version, "revision", max_attempts=settings.ingest_max_attempts
        )
    return "obsolete"


async def _embedded_chunks(
    chunks: list[dict[str, Any]], stop: asyncio.Event | None
) -> AsyncIterator[dict[str, Any]]:
    async with Embedder() as embedder:
        for offset in range(0, len(chunks), 64):
            _raise_if_stopping(stop)
            batch = chunks[offset : offset + 64]
            vectors = await embedder.embed_many([str(chunk["content_text"]) for chunk in batch])
            if len(vectors) != len(batch):
                raise ResourceTemporaryError("Embedding response count did not match chunks")
            for chunk, vector in zip(batch, vectors, strict=True):
                yield {**chunk, "content_embedding": vector}


class InvalidIngestionJobError(Exception):
    """A job violates an invariant and must not be retried."""


class IngestionShutdownRequestedError(Exception):
    """The platform requested a cooperative stop at a safe stage boundary."""


class IngestionLeaseLostError(Exception):
    """The current worker can no longer safely commit the claimed job."""


@dataclass(frozen=True, slots=True)
class DrainResult:
    """Machine-readable outcome for one scheduled drain task."""

    reason: Literal["idle", "max_runtime", "signal"]
    jobs_processed: int
    elapsed_seconds: float

    def as_dict(self) -> dict[str, str | int | float]:
        return {
            "reason": self.reason,
            "jobs_processed": self.jobs_processed,
            "elapsed_seconds": self.elapsed_seconds,
        }


def _raise_if_stopping(stop: asyncio.Event | None) -> None:
    if stop is not None and stop.is_set():
        raise IngestionShutdownRequestedError


def _safe_error(exc: Exception) -> str:
    message = _ERROR_SECRET.sub(r"\1[redacted]", str(exc))
    message = message.replace(str(_SCRATCH_ROOT), "/data/ingestion")
    return message[:1000] or type(exc).__name__


def _retry_delay(attempt: int) -> dt.timedelta:
    seconds = min(300 * (2 ** max(attempt - 1, 0)), 24 * 60 * 60)
    return dt.timedelta(seconds=seconds)


async def _parse_resource(
    job: IngestionJob,
    resource: DownloadedResource,
    scratch: Path,
    stop_event: asyncio.Event | None = None,
) -> tuple[str, str, dict[str, bool]]:
    if resource.kind == "pdf":
        markdown = await _work_io(pdf_to_markdown, resource.path, fast=True)
        _raise_if_stopping(stop_event)
        if not markdown.strip():
            raise ResourceCorruptError("PDF parser produced empty markdown")
        return (
            markdown,
            "pdf",
            {
                "has_pdf": True,
                "has_latex": False,
                "has_markdown": True,
            },
        )
    if resource.kind != "latex":
        raise ResourceCorruptError("Downloaded resource has an unsupported type")

    try:
        markdown = await _work_io(latex_to_markdown, resource.path)
        _raise_if_stopping(stop_event)
        if not markdown.strip():
            raise LatexMdError("LaTeX parser produced empty markdown")
    except LatexMdError as exc:
        if isinstance(exc, LatexResourceLimitError):
            emit_emf(
                service="paper-ingest",
                outcome="fallback",
                metrics={"PandocResourceFallback": (1, "Count")},
            )
        logger.info(
            "latex parse failed; falling back to exact PDF",
            arxiv_id=job.arxiv_id,
            target_version=job.target_version,
            error_type=type(exc).__name__,
        )
        pdf = await _work_io(
            fetch_pdf_resource,
            job.arxiv_id,
            job.target_version,
            scratch,
        )
        _raise_if_stopping(stop_event)
        markdown = await _work_io(pdf_to_markdown, pdf.path, fast=True)
        _raise_if_stopping(stop_event)
        if not markdown.strip():
            raise ResourceCorruptError("PDF fallback parser produced empty markdown") from None
        return (
            markdown,
            "pdf",
            {
                "has_pdf": True,
                "has_latex": False,
                "has_markdown": True,
            },
        )
    return (
        markdown,
        "latex",
        {
            "has_pdf": False,
            "has_latex": True,
            "has_markdown": True,
        },
    )


async def process_job(
    job: IngestionJob,
    *,
    scratch_root: Path = _SCRATCH_ROOT,
    stop_event: asyncio.Event | None = None,
) -> str:
    """Build and safely install one exact revision. Return ``installed`` or ``obsolete``."""
    require_full_runtime("Background generation")
    canonical = canonicalize_arxiv_id(job.arxiv_id)
    if canonical is None or canonical != job.arxiv_id:
        raise InvalidIngestionJobError("Job contains an invalid canonical arXiv ID")

    paper = await _work_io(get_paper, job.arxiv_id)
    _raise_if_stopping(stop_event)
    if paper is None:
        raise InvalidIngestionJobError("Paper metadata no longer exists")
    current_version = max(int(paper.get("version") or 1), 1)
    if current_version > job.target_version:
        return await _target_obsolete(job, current_version)
    if current_version != job.target_version:
        raise InvalidIngestionJobError("Job target version does not match paper metadata")

    safe_id = job.arxiv_id.replace("/", "_")
    scratch = (scratch_root / safe_id / f"v{job.target_version}").resolve()
    if not scratch.is_relative_to(scratch_root.resolve()):
        raise InvalidIngestionJobError("Scratch path escaped its root")
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)

    try:
        if configured_queue() is not None:
            async with target_install(
                job, scratch / "recovery", checkpoint=lambda: _raise_if_stopping(stop_event)
            ) as install:
                if await install.exists():
                    await install.apply()
                    return "installed"
        resource = await _work_io(
            fetch_paper_resource,
            job.arxiv_id,
            job.target_version,
            scratch,
        )
        _raise_if_stopping(stop_event)
        markdown, source, resource_flags = await _parse_resource(
            job,
            resource,
            scratch,
            stop_event,
        )

        parsed = chunk_markdown(markdown, source=source)
        _raise_if_stopping(stop_event)
        chunks: list[dict[str, Any]] = []
        for item in parsed:
            content = truncate_utf8(item.content, 16384)
            if content.strip():
                chunks.append(
                    {
                        "chunk_id": f"{job.arxiv_id}::chunk::{item.chunk_index}",
                        "arxiv_id": job.arxiv_id,
                        "chunk_idx": item.chunk_index,
                        "content_text": content,
                    }
                )
        if not chunks:
            raise ResourceCorruptError("Chunker produced no usable chunks")
        if configured_queue() is not None:
            for chunk in chunks:
                chunk["chunk_id"] = (
                    f"{job.arxiv_id}::v{job.target_version}::{fulltext_profile_hash()[:12]}::{chunk['chunk_idx']}"
                )
            async with target_install(
                job, scratch / "recovery", checkpoint=lambda: _raise_if_stopping(stop_event)
            ) as install:
                await install.prepare(_embedded_chunks(chunks, stop_event), resource_flags)
                _raise_if_stopping(stop_event)
                await install.apply()
            return "installed"
        async with Embedder() as embedder:
            vectors = await embedder.embed_many([str(chunk["content_text"]) for chunk in chunks])
        _raise_if_stopping(stop_event)
        if len(vectors) != len(chunks):
            raise ResourceTemporaryError("Embedding response count did not match chunks")
        for chunk, vector in zip(chunks, vectors):
            chunk["content_embedding"] = vector
        latest = await _work_io(get_paper, job.arxiv_id)
        _raise_if_stopping(stop_event)
        if latest is None:
            raise InvalidIngestionJobError("Paper metadata disappeared during ingestion")
        if max(int(latest.get("version") or 1), 1) > job.target_version:
            return await _target_obsolete(job, max(int(latest.get("version") or 1), 1))
        _raise_if_stopping(stop_event)
        await _work_io(
            install_paper_chunks,
            job.arxiv_id,
            chunks,
            target_version=job.target_version,
            resource_flags=resource_flags,
        )
        _raise_if_stopping(stop_event)
        return "installed"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        with contextlib.suppress(OSError):
            scratch.parent.rmdir()


async def _maintain_lease(
    job: IngestionJob,
    worker_id: str,
    finished: asyncio.Event,
    heartbeat_interval_seconds: float,
) -> None:
    while True:
        try:
            await asyncio.wait_for(finished.wait(), timeout=heartbeat_interval_seconds)
            return
        except TimeoutError:
            pass
        renewed = await renew_ingestion_job_lease(
            job.arxiv_id,
            worker_id,
            settings.ingest_lease_seconds,
        )
        if not renewed:
            raise IngestionLeaseLostError("Ingestion job lease is no longer owned by this worker")


async def _process_with_heartbeat(
    job: IngestionJob,
    worker_id: str,
    *,
    scratch_root: Path,
    stop_event: asyncio.Event | None,
    heartbeat_interval_seconds: float,
) -> str:
    finished = asyncio.Event()
    process_task = asyncio.create_task(
        process_job(job, scratch_root=scratch_root, stop_event=stop_event)
    )
    heartbeat_task = asyncio.create_task(
        _maintain_lease(job, worker_id, finished, heartbeat_interval_seconds)
    )
    try:
        done, _ = await asyncio.wait(
            {process_task, heartbeat_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if heartbeat_task in done:
            error = heartbeat_task.exception()
            if error is not None:
                process_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await process_task
                raise error
        return await process_task
    finally:
        if not process_task.done():
            process_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await process_task
        finished.set()
        await heartbeat_task


async def run_worker_once(
    worker_id: str,
    *,
    scratch_root: Path = _SCRATCH_ROOT,
    stop_event: asyncio.Event | None = None,
    heartbeat_interval_seconds: float | None = None,
    max_processing_seconds: float | None = None,
) -> bool:
    require_full_runtime("Background generation")
    if stop_event is not None and stop_event.is_set():
        return False
    await verified_sync_source()
    job = await claim_ingestion_job(worker_id, settings.ingest_lease_seconds)
    if job is None:
        return False
    worker_id = job.lease_owner or worker_id
    interval = heartbeat_interval_seconds or min(60.0, settings.ingest_lease_seconds / 3)
    try:
        processing = _process_with_heartbeat(
            job,
            worker_id,
            scratch_root=scratch_root,
            stop_event=stop_event,
            heartbeat_interval_seconds=interval,
        )
        if max_processing_seconds is None:
            outcome = await processing
        else:
            deadline = asyncio.timeout(max_processing_seconds)
            try:
                async with deadline:
                    outcome = await processing
            except TimeoutError as exc:
                if deadline.expired():
                    # The task's scheduled slot ended; joined processing can
                    # return its lease without spending a paper's retry budget.
                    raise IngestionShutdownRequestedError from exc
                raise
        if outcome == "obsolete" and configured_queue() is not None:
            return True
        await complete_ingestion_job(job.arxiv_id, worker_id)
        logger.info(
            "ingestion job completed",
            arxiv_id=job.arxiv_id,
            target_version=job.target_version,
            outcome=outcome,
        )
        emit_emf(
            service="paper-ingest",
            outcome="succeeded",
            metrics={"IngestionSucceeded": (1, "Count")},
        )
    except IngestionShutdownRequestedError:
        released = await release_ingestion_job(job.arxiv_id, worker_id)
        logger.info(
            "ingestion job released after shutdown request",
            arxiv_id=job.arxiv_id,
            released=released,
        )
        emit_emf(
            service="paper-ingest",
            outcome="released",
            metrics={"IngestionReleased": (1, "Count")},
        )
    except (IngestionLeaseLostError, TargetLeaseLostError):
        logger.warning("ingestion job lease lost", arxiv_id=job.arxiv_id)
        emit_emf(
            service="paper-ingest",
            outcome="lease_lost",
            metrics={"IngestionLeaseLost": (1, "Count")},
        )
    except (InvalidIngestionJobError, IngestionSafetyError) as exc:
        await fail_ingestion_job(
            job.arxiv_id,
            worker_id,
            code="invalid_job",
            message=_safe_error(exc),
            retry_at=None,
        )
        emit_emf(
            service="paper-ingest",
            outcome="dead",
            metrics={"IngestionDead": (1, "Count")},
        )
    except (ResourceUnavailableError, ResourceCorruptError, LatexMdError, PDFMdError) as exc:
        retry_at = (
            dt.datetime.now(dt.UTC) + _retry_delay(job.attempt_count)
            if job.attempt_count < min(job.max_attempts, 3)
            else None
        )
        await fail_ingestion_job(
            job.arxiv_id,
            worker_id,
            code="source_or_parse_failure",
            message=_safe_error(exc),
            retry_at=retry_at,
        )
        emit_emf(
            service="paper-ingest",
            outcome="retry" if retry_at is not None else "dead",
            metrics={("IngestionRetry" if retry_at is not None else "IngestionDead"): (1, "Count")},
        )
    except Exception as exc:
        retry_at = (
            dt.datetime.now(dt.UTC) + _retry_delay(job.attempt_count)
            if job.attempt_count < job.max_attempts
            else None
        )
        await fail_ingestion_job(
            job.arxiv_id,
            worker_id,
            code="temporary_failure",
            message=_safe_error(exc),
            retry_at=retry_at,
        )
        emit_emf(
            service="paper-ingest",
            outcome="retry" if retry_at is not None else "dead",
            metrics={("IngestionRetry" if retry_at is not None else "IngestionDead"): (1, "Count")},
        )
    return True


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(name, stop.set)


async def drain_ingest(
    *,
    idle_grace_seconds: float = 60,
    max_runtime_seconds: float = 30 * 60,
    poll_seconds: float = _POLL_SECONDS,
    stop_event: asyncio.Event | None = None,
) -> DrainResult:
    """Drain available work, then exit on idle, deadline, or platform signal."""
    require_full_runtime("Full-text ingestion")
    stop = stop_event or asyncio.Event()
    if stop_event is None:
        _install_signal_handlers(stop)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    started = time.monotonic()
    last_work = started
    jobs_processed = 0

    while True:
        now = time.monotonic()
        elapsed = now - started
        if stop.is_set():
            reason: Literal["idle", "max_runtime", "signal"] = "signal"
            break
        if elapsed >= max_runtime_seconds:
            reason = "max_runtime"
            break

        worked = await run_worker_once(
            worker_id,
            stop_event=stop,
            max_processing_seconds=max(max_runtime_seconds - elapsed, 0.001),
        )
        now = time.monotonic()
        if worked:
            jobs_processed += 1
            last_work = now
            continue

        idle_remaining = idle_grace_seconds - (now - last_work)
        runtime_remaining = max_runtime_seconds - (now - started)
        if idle_remaining <= 0:
            reason = "idle"
            break
        if runtime_remaining <= 0:
            reason = "max_runtime"
            break
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                stop.wait(),
                timeout=min(poll_seconds, idle_remaining, runtime_remaining),
            )

    result = DrainResult(
        reason=reason,
        jobs_processed=jobs_processed,
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
    logger.info("ingestion drain finished", **result.as_dict())
    emit_emf(
        service="paper-ingest",
        outcome=reason,
        metrics={"IngestionDrainJobs": (jobs_processed, "Count")},
    )
    try:
        queue = (await get_ingestion_status())["queue"]
        emit_emf(
            service="paper-ingest",
            metrics={
                "IngestionBacklog": (int(queue["backlog"]), "Count"),
                "IngestionDeadTotal": (int(queue["dead"]), "Count"),
                "IngestionOldestAge": (int(queue["oldest_age_seconds"]), "Seconds"),
            },
        )
    except Exception:
        logger.exception("ingestion queue metrics unavailable")
    return result


async def serve_ingest() -> None:
    """Claim and process jobs until SIGINT/SIGTERM."""
    require_full_runtime("Full-text ingestion")
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    while not stop.is_set():
        worked = await run_worker_once(worker_id, stop_event=stop)
        if not worked:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=_POLL_SECONDS)
