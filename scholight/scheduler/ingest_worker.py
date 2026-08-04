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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import structlog

from scholight.config import settings
from scholight.db.queries_ingestion import (
    IngestionJob,
    claim_ingestion_job,
    complete_ingestion_job,
    fail_ingestion_job,
    get_ingestion_status,
    release_ingestion_job,
    renew_ingestion_job_lease,
)
from scholight.logging.emf import emit_emf
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
from scholight.store.ingestion import (
    IngestionSafetyError,
    get_paper,
    install_paper_chunks,
)

logger = structlog.get_logger(__name__)
_SCRATCH_ROOT = Path("/data/ingestion")
_POLL_SECONDS = 10
_ERROR_SECRET = re.compile(r"(?i)(token|key|password|authorization)[=:]\\s*\\S+")


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
    message = _ERROR_SECRET.sub(r"\1=[redacted]", str(exc))
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
        markdown = await asyncio.to_thread(pdf_to_markdown, resource.path, fast=True)
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
        markdown = await asyncio.to_thread(latex_to_markdown, resource.path)
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
        pdf = await asyncio.to_thread(
            fetch_pdf_resource,
            job.arxiv_id,
            job.target_version,
            scratch,
        )
        _raise_if_stopping(stop_event)
        markdown = await asyncio.to_thread(pdf_to_markdown, pdf.path, fast=True)
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
    canonical = canonicalize_arxiv_id(job.arxiv_id)
    if canonical is None or canonical != job.arxiv_id:
        raise InvalidIngestionJobError("Job contains an invalid canonical arXiv ID")

    paper = await asyncio.to_thread(get_paper, job.arxiv_id)
    _raise_if_stopping(stop_event)
    if paper is None:
        raise InvalidIngestionJobError("Paper metadata no longer exists")
    current_version = max(int(paper.get("version") or 1), 1)
    if current_version > job.target_version:
        return "obsolete"
    if current_version != job.target_version:
        raise InvalidIngestionJobError("Job target version does not match paper metadata")

    safe_id = job.arxiv_id.replace("/", "_")
    scratch = (scratch_root / safe_id / f"v{job.target_version}").resolve()
    if not scratch.is_relative_to(scratch_root.resolve()):
        raise InvalidIngestionJobError("Scratch path escaped its root")
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)

    try:
        resource = await asyncio.to_thread(
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
            content = item.content[:16384]
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
        async with Embedder() as embedder:
            vectors = await embedder.embed_many([str(chunk["content_text"]) for chunk in chunks])
        _raise_if_stopping(stop_event)
        if len(vectors) != len(chunks):
            raise ResourceTemporaryError("Embedding response count did not match chunks")
        for chunk, vector in zip(chunks, vectors):
            chunk["content_embedding"] = vector
        latest = await asyncio.to_thread(get_paper, job.arxiv_id)
        _raise_if_stopping(stop_event)
        if latest is None:
            raise InvalidIngestionJobError("Paper metadata disappeared during ingestion")
        if max(int(latest.get("version") or 1), 1) > job.target_version:
            return "obsolete"
        _raise_if_stopping(stop_event)
        await asyncio.to_thread(
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
    if stop_event is not None and stop_event.is_set():
        return False
    job = await claim_ingestion_job(worker_id, settings.ingest_lease_seconds)
    if job is None:
        return False
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
            async with asyncio.timeout(max_processing_seconds):
                outcome = await processing
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
    except IngestionLeaseLostError:
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
    max_runtime_seconds: float = 110 * 60,
    poll_seconds: float = _POLL_SECONDS,
    stop_event: asyncio.Event | None = None,
) -> DrainResult:
    """Drain available work, then exit on idle, deadline, or platform signal."""
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
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    while not stop.is_set():
        worked = await run_worker_once(worker_id, stop_event=stop)
        if not worked:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=_POLL_SECONDS)
