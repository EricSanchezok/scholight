"""Continuous, cursor-driven arXiv metadata synchronization."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import signal
from typing import Any

import structlog

from scholight.config import settings
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
_ZERO_GUARD_DAYS = 5


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


async def _fetch_day(date: dt.date, reference: dt.date) -> tuple[list[dict[str, Any]], str]:
    date_string = date.isoformat()
    for base, label in ((OAI_PRIMARY, "oai"), (OAI_FALLBACK, "oai_fallback")):
        if not await oai_health_check(base):
            continue
        try:
            papers = await iter_papers_oai(date_string, date_string, base=base)
            if papers or (reference - date).days > _ZERO_GUARD_DAYS:
                return papers, label
        except Exception as exc:
            logger.warning("metadata source failed", source=label, error=type(exc).__name__)
    papers = await fetch_papers_api(date)
    return papers, "api"


async def _sync_day(date: dt.date, reference: dt.date) -> tuple[int, str]:
    papers, source = await _fetch_day(date, reference)
    await _normalize_and_embed(papers)
    outcomes = await asyncio.to_thread(write_metadata_papers, papers)
    for paper, outcome in zip(papers, outcomes, strict=True):
        kind = outcome.kind
        # Heal the cross-store interruption where Zilliz accepted a revision
        # but PostgreSQL was unavailable before its durable job was created.
        # Re-enqueueing is idempotent for an existing succeeded job.
        if kind is None and bool(paper.get("_version_available")) and outcome.target_version > 1:
            kind = "revision"
        if kind is None:
            continue
        await enqueue_ingestion_job(
            outcome.arxiv_id,
            outcome.target_version,
            kind,
            max_attempts=settings.ingest_max_attempts,
        )
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

    reconciled = await _reconcile_recent(yesterday)
    return {
        "papers": total,
        "days": days,
        "failed_date": None,
        "reconciled": reconciled,
        "sources": sources,
    }


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
        try:
            await run_sync()
        except Exception:
            logger.exception("metadata sync cycle crashed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                stop.wait(),
                timeout=_seconds_until_sync(dt.datetime.now(dt.UTC)),
            )
