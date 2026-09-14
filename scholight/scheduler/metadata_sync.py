"""Continuous, cursor-driven arXiv metadata synchronization."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import signal
from typing import Any

import structlog

from scholight.config import settings
from scholight.db.queries_deferred_fulltext import record_deferred_fulltext
from scholight.db.queries_ingestion import (
    enqueue_ingestion_job,
    get_sync_state,
    initialize_sync_cursor,
    mark_sync_failed,
    mark_sync_started,
    mark_sync_succeeded,
)
from scholight.pipeline.embedder import Embedder
from scholight.sources.arxiv import (
    OAI_FALLBACK,
    OAI_PRIMARY,
    fetch_papers_api,
    iter_papers_oai,
    oai_health_check,
)
from scholight.store.ingestion import (
    list_missing_chunks,
    paper_exists_on_date,
    write_metadata_papers,
)
from scholight.utils.text import truncate_utf8

logger = structlog.get_logger(__name__)
_SOURCE = "arxiv"


class IncompleteMetadataCoverageError(RuntimeError):
    """Observed new submissions do not prove complete daily revision coverage."""


async def _normalize_and_embed(papers: list[dict[str, Any]]) -> None:
    non_empty = [
        (index, str(paper.get("abstract") or ""))
        for index, paper in enumerate(papers)
        if str(paper.get("abstract") or "").strip()
    ]
    if non_empty:
        async with Embedder() as embedder:
            vectors = await embedder.embed_many([text for _, text in non_empty])
        if len(vectors) != len(non_empty):
            raise RuntimeError("Embedding response count did not match metadata")
        for (index, _), vector in zip(non_empty, vectors):
            papers[index]["abstract_embedding"] = vector
            available_fields = papers[index].get("_metadata_fields")
            if isinstance(available_fields, set):
                available_fields.add("abstract_embedding")
    for paper in papers:
        paper["abstract_embedding"] = paper.get("abstract_embedding") or (
            [0.0] * settings.embedding_dim
        )
        paper.pop("abstract_bm25", None)
        paper.setdefault("authors", [])
        paper.setdefault("categories", [])
        paper.setdefault("created", paper.get("updated") or "")
        paper.setdefault("updated", paper.get("created") or "")
        paper.setdefault("version", 1)
        paper.setdefault("updated_history", [])
        for flag in ("has_latex", "has_pdf", "has_markdown", "has_chunks"):
            paper.setdefault(flag, False)
        for key, default in (
            ("license", ""),
            ("comments", ""),
            ("doi", ""),
            ("journal_ref", ""),
            ("acm_class", ""),
        ):
            paper.setdefault(key, default)
        for key, size in (
            ("title", 2048),
            ("abstract", 16384),
            ("created", 16),
            ("updated", 16),
            ("license", 512),
            ("comments", 8192),
            ("doi", 256),
            ("journal_ref", 2048),
            ("acm_class", 256),
        ):
            paper[key] = truncate_utf8(str(paper.get(key) or ""), size)
        paper["authors"] = [truncate_utf8(str(author), 256) for author in paper["authors"]]
        paper["updated_history"] = [
            truncate_utf8(str(value), 16) for value in paper["updated_history"] if value
        ]


async def _fetch_day(date: dt.date, _reference: dt.date) -> tuple[list[dict[str, Any]], str]:
    date_string = date.isoformat()
    for base, label in ((OAI_PRIMARY, "oai"), (OAI_FALLBACK, "oai_fallback")):
        if not await oai_health_check(base):
            continue
        try:
            papers = await iter_papers_oai(date_string, date_string, base=base)
            return papers, label
        except Exception as exc:
            logger.warning("metadata source failed", source=label, error=type(exc).__name__)
    papers = await fetch_papers_api(date)
    return papers, "api"


async def _write_batch(papers: list[dict[str, Any]], date: dt.date) -> None:
    await _normalize_and_embed(papers)
    outcomes = await asyncio.to_thread(write_metadata_papers, papers)
    if len(outcomes) != len(papers):
        raise RuntimeError("Metadata write outcome count did not match batch")
    if settings.runtime_profile == "lean":
        await record_deferred_fulltext(
            [(outcome.arxiv_id, outcome.target_version) for outcome in outcomes], date
        )
        return
    for paper, outcome in zip(papers, outcomes, strict=True):
        if paper.get("_version_available") is False:
            raise IncompleteMetadataCoverageError("Source did not provide an exact paper version")
        kind = outcome.kind
        # Replay must also heal the first v1 write when PostgreSQL registration
        # failed after the vector store accepted that new paper.
        if kind is None and bool(paper.get("_version_available")):
            kind = "new" if outcome.target_version == 1 else "revision"
        if kind is None:
            continue
        await enqueue_ingestion_job(
            outcome.arxiv_id,
            outcome.target_version,
            kind,
            max_attempts=settings.ingest_max_attempts,
        )


async def _sync_day(date: dt.date, reference: dt.date) -> tuple[int, str]:
    papers, source = await _fetch_day(date, reference)
    size = settings.metadata_sync_batch_size
    for offset in range(0, len(papers), size):
        # Vectors belong only to this short-lived batch, never to the source
        # day's records. A failed batch replays the day before its cursor moves.
        batch = [dict(paper) for paper in papers[offset : offset + size]]
        await _write_batch(batch, date)
        del batch
    return len(papers), source


async def _initial_cursor(yesterday: dt.date) -> dt.date:
    window_start = yesterday - dt.timedelta(days=settings.ingest_recent_days - 1)
    probe = yesterday
    while probe >= window_start:
        if await asyncio.to_thread(paper_exists_on_date, probe.isoformat()):
            return probe
        probe -= dt.timedelta(days=1)
    return window_start - dt.timedelta(days=1)


async def _reconcile_recent(yesterday: dt.date) -> int:
    start = yesterday - dt.timedelta(days=settings.ingest_recent_days - 1)
    rows = await asyncio.to_thread(
        list_missing_chunks,
        start.isoformat(),
        yesterday.isoformat(),
        10_000,
    )
    for row in rows:
        await enqueue_ingestion_job(
            str(row["arxiv_id"]),
            max(int(row.get("version") or 1), 1),
            "reconciliation",
            max_attempts=settings.ingest_max_attempts,
        )
    return len(rows)


async def run_sync(*, today: dt.date | None = None) -> dict[str, Any]:
    """Synchronize consecutive days through UTC yesterday; stop at first failure."""
    utc_today = today or dt.datetime.now(dt.UTC).date()
    yesterday = utc_today - dt.timedelta(days=1)
    await mark_sync_started(_SOURCE)
    state = await get_sync_state(_SOURCE)
    if state is None or state.last_successful_date is None:
        cursor = await _initial_cursor(yesterday)
        await initialize_sync_cursor(_SOURCE, cursor)
    else:
        cursor = state.last_successful_date

    total = 0
    days = 0
    sources: dict[str, int] = {"oai": 0, "oai_fallback": 0, "api": 0}
    current = cursor + dt.timedelta(days=1)
    while current <= yesterday:
        try:
            count, source = await _sync_day(current, yesterday)
            if source == "api":
                raise IncompleteMetadataCoverageError(
                    "Atom submission-date fallback does not cover all revisions; retry OAI"
                )
            await mark_sync_succeeded(_SOURCE, current)
        except Exception as exc:
            await mark_sync_failed(_SOURCE, type(exc).__name__, str(exc)[:1000])
            logger.exception("metadata day failed; cursor not advanced", date=current.isoformat())
            return {
                "papers": total,
                "days": days,
                "failed_date": current.isoformat(),
                "sources": sources,
            }
        total += count
        days += 1
        sources[source] += 1
        current += dt.timedelta(days=1)

    reconciled = await _reconcile_recent(yesterday) if settings.runtime_profile == "full" else 0
    return {
        "papers": total,
        "days": days,
        "failed_date": None,
        "reconciled": reconciled,
        "sources": sources,
    }


async def run_exclusive_sync() -> dict[str, Any]:
    """Bound the scheduled task and serialize overlapping runs in PostgreSQL."""
    from scholight.db.client import bind_pool_connection, get_pool

    async with bind_pool_connection(get_pool()) as connection:
        acquired = await connection.fetchval("SELECT pg_try_advisory_lock($1)", 7192003902)
        if not acquired:
            return {"skipped": "sync_already_running", "failed_date": None}
        try:
            async with asyncio.timeout(settings.metadata_sync_timeout_seconds):
                return await run_sync()
        finally:
            await connection.execute("SELECT pg_advisory_unlock($1)", 7192003902)


async def run_sync_command() -> dict[str, Any]:
    """Cancel on SIGTERM so the current uncommitted day is replayed next run."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("Metadata synchronization requires an asyncio task")
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        return await run_exclusive_sync()
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def _seconds_until_sync(now: dt.datetime) -> float:
    target = now.replace(
        hour=settings.metadata_sync_hour_utc,
        minute=0,
        second=0,
        microsecond=0,
    )
    if target <= now:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds()


async def serve_sync() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stop.set)
    while not stop.is_set():
        cycle = asyncio.create_task(run_exclusive_sync())
        stopped = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({cycle, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if stop.is_set():
                cycle.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cycle
                return
            await cycle
        except Exception:
            logger.exception("metadata sync cycle crashed")
        finally:
            stopped.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopped
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                stop.wait(),
                timeout=_seconds_until_sync(dt.datetime.now(dt.UTC)),
            )
