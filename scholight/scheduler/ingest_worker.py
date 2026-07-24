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
from pathlib import Path
from typing import Any

import structlog

from scholight.config import settings
from scholight.db.queries_ingestion import (
    IngestionJob,
    claim_ingestion_job,
    complete_ingestion_job,
    fail_ingestion_job,
)
from scholight.pipeline.chunkers.md_chunker import chunk_markdown
from scholight.pipeline.embedder import Embedder
from scholight.pipeline.latex_md import LatexMdError, latex_to_markdown
from scholight.pipeline.pdf_md import PDFMdError, pdf_to_markdown
from scholight.scheduler.resources import (
    ResourceCorruptError,
    ResourceTemporaryError,
    ResourceUnavailableError,
    fetch_paper_resource,
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


def _safe_error(exc: Exception) -> str:
    message = _ERROR_SECRET.sub(r"\1=[redacted]", str(exc))
    message = message.replace(str(_SCRATCH_ROOT), "/data/ingestion")
    return message[:1000] or type(exc).__name__


def _retry_delay(attempt: int) -> dt.timedelta:
    seconds = min(300 * (2 ** max(attempt - 1, 0)), 24 * 60 * 60)
    return dt.timedelta(seconds=seconds)


async def process_job(job: IngestionJob, *, scratch_root: Path = _SCRATCH_ROOT) -> str:
    """Build and safely install one exact revision. Return ``installed`` or ``obsolete``."""
    canonical = canonicalize_arxiv_id(job.arxiv_id)
    if canonical is None or canonical != job.arxiv_id:
        raise InvalidIngestionJobError("Job contains an invalid canonical arXiv ID")

    paper = await asyncio.to_thread(get_paper, job.arxiv_id)
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

    resource_flags = {"has_pdf": False, "has_latex": False, "has_markdown": True}
    try:
        resource = await asyncio.to_thread(
            fetch_paper_resource,
            job.arxiv_id,
            job.target_version,
            scratch,
        )
        if resource.kind == "latex":
            markdown = await asyncio.to_thread(latex_to_markdown, resource.path)
            resource_flags["has_latex"] = True
            source = "latex"
        else:
            markdown = await asyncio.to_thread(pdf_to_markdown, resource.path, fast=True)
            resource_flags["has_pdf"] = True
            source = "pdf"
        if not markdown.strip():
            raise ResourceCorruptError("Parser produced empty markdown")

        parsed = chunk_markdown(markdown, source=source)
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
        if len(vectors) != len(chunks):
            raise ResourceTemporaryError("Embedding response count did not match chunks")
        for chunk, vector in zip(chunks, vectors):
            chunk["content_embedding"] = vector
        latest = await asyncio.to_thread(get_paper, job.arxiv_id)
        if latest is None:
            raise InvalidIngestionJobError("Paper metadata disappeared during ingestion")
        if max(int(latest.get("version") or 1), 1) > job.target_version:
            return "obsolete"
        await asyncio.to_thread(
            install_paper_chunks,
            job.arxiv_id,
            chunks,
            target_version=job.target_version,
            resource_flags=resource_flags,
        )
        return "installed"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


async def run_worker_once(worker_id: str) -> bool:
    job = await claim_ingestion_job(worker_id, settings.ingest_lease_seconds)
    if job is None:
        return False
    try:
        outcome = await process_job(job)
        await complete_ingestion_job(job.arxiv_id, worker_id)
        logger.info(
            "ingestion job completed",
            arxiv_id=job.arxiv_id,
            target_version=job.target_version,
            outcome=outcome,
        )
    except (InvalidIngestionJobError, IngestionSafetyError) as exc:
        await fail_ingestion_job(
            job.arxiv_id,
            worker_id,
            code="invalid_job",
            message=_safe_error(exc),
            retry_at=None,
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
    return True


async def serve_ingest() -> None:
    """Claim and process jobs until SIGINT/SIGTERM."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stop.set)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    while not stop.is_set():
        worked = await run_worker_once(worker_id)
        if not worked:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=_POLL_SECONDS)
