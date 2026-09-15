"""CLI for durable metadata synchronization and paper ingestion."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import Awaitable, Callable
from typing import Any

import click

from scholight.config import require_full_runtime, settings
from scholight.db.client import close_pool, create_pool


@click.group("scheduler")
def scheduler_group() -> None:
    """Run and inspect the native arXiv ingestion system."""


async def _with_pool(function: Callable[[], Awaitable[Any]]) -> Any:
    await create_pool()
    try:
        return await function()
    finally:
        await close_pool()


@scheduler_group.command("sync")
def sync_cmd() -> None:
    """Run one cursor-driven metadata synchronization."""
    from scholight.scheduler.metadata_sync import run_sync_command

    result = asyncio.run(_with_pool(run_sync_command))
    click.echo(json.dumps(result, default=str, sort_keys=True))
    if result.get("failed_date"):
        raise click.ClickException(f"sync stopped at {result['failed_date']}")


@scheduler_group.command("serve-sync")
def serve_sync_cmd() -> None:
    """Run metadata sync immediately, then daily at the configured UTC hour."""
    from scholight.scheduler.metadata_sync import serve_sync

    asyncio.run(_with_pool(serve_sync))


@scheduler_group.command("serve-ingest")
def serve_ingest_cmd() -> None:
    """Run the single-paper ingestion worker."""
    require_full_runtime("Full-text ingestion")
    from scholight.scheduler.ingest_worker import serve_ingest

    asyncio.run(_with_pool(serve_ingest))


@scheduler_group.command("drain-ingest")
@click.option(
    "--idle-grace-seconds",
    type=click.IntRange(1, 600),
    default=60,
    show_default=True,
)
@click.option(
    "--max-runtime-seconds",
    type=click.IntRange(60, 7200),
    default=30 * 60,
    show_default=True,
)
@click.option(
    "--concurrency",
    type=click.IntRange(1, 4),
    default=None,
    help="Paper lanes (defaults to SCHOLIGHT_INGEST_CONCURRENCY).",
)
def drain_ingest_cmd(
    idle_grace_seconds: int, max_runtime_seconds: int, concurrency: int | None
) -> None:
    """Drain the paper queue for one bounded scheduled task."""
    require_full_runtime("Full-text ingestion")
    from scholight.scheduler.ingest_worker import drain_ingest

    async def _run() -> dict[str, str | int | float]:
        result = await drain_ingest(
            idle_grace_seconds=idle_grace_seconds,
            max_runtime_seconds=max_runtime_seconds,
            concurrency=concurrency,
        )
        return result.as_dict()

    result = asyncio.run(_with_pool(_run))
    click.echo(json.dumps(result, sort_keys=True))


@scheduler_group.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def status_cmd(as_json: bool) -> None:
    """Show PostgreSQL queue and continuous-sync state."""
    from scholight.db.ingestion import get_ingestion_status

    result = asyncio.run(_with_pool(get_ingestion_status))
    if as_json:
        click.echo(json.dumps(result, default=str, sort_keys=True))
        return
    sync = result.get("sync")
    click.echo(f"Last consecutive date: {(sync or {}).get('last_successful_date') or 'never'}")
    for status in ("pending", "running", "retry", "succeeded", "dead"):
        click.echo(f"{status:>10}: {result['jobs'].get(status, 0):>8}")


@scheduler_group.command("enqueue-backfill")
@click.option("from_date", "--from", type=click.DateTime(formats=["%Y-%m-%d"]), required=True)
@click.option("to_date", "--to", type=click.DateTime(formats=["%Y-%m-%d"]), required=True)
@click.option("--limit", type=click.IntRange(1, 10_000), default=500, show_default=True)
@click.option("--apply", is_flag=True, help="Write jobs; otherwise perform a dry run.")
def enqueue_backfill_cmd(
    from_date: dt.datetime,
    to_date: dt.datetime,
    limit: int,
    apply: bool,
) -> None:
    """Find old papers without chunks and optionally enqueue a bounded batch."""
    require_full_runtime("Full-text ingestion")
    if from_date.date() > to_date.date():
        raise click.UsageError("--from must be on or before --to")

    async def _run() -> dict[str, Any]:
        from scholight.db.ingestion import configured_queue, enqueue_ingestion_job

        if configured_queue() is not None:
            raise click.UsageError("Bound targets use resume-fulltext with a reviewed scope")
        from scholight.store.ingestion import list_missing_chunks

        rows = await asyncio.to_thread(
            list_missing_chunks,
            from_date.date().isoformat(),
            to_date.date().isoformat(),
            limit,
        )
        changed = 0
        if apply:
            for row in rows:
                changed += int(
                    await enqueue_ingestion_job(
                        str(row["arxiv_id"]),
                        max(int(row.get("version") or 1), 1),
                        "backfill",
                        max_attempts=settings.ingest_max_attempts,
                    )
                )
        return {"matched": len(rows), "enqueued": changed, "dry_run": not apply}

    click.echo(json.dumps(asyncio.run(_with_pool(_run)), sort_keys=True))


@scheduler_group.command("retry")
@click.option("--arxiv-id", required=True)
def retry_cmd(arxiv_id: str) -> None:
    """Reactivate or explicitly enqueue one paper by exact arXiv ID."""
    require_full_runtime("Full-text ingestion")
    from scholight.db.ingestion import (
        enqueue_ingestion_job,
        get_ingestion_job,
        retry_ingestion_job,
    )
    from scholight.sources.arxiv import canonicalize_arxiv_id
    from scholight.store.ingestion import get_paper

    if canonicalize_arxiv_id(arxiv_id) != arxiv_id:
        raise click.UsageError("--arxiv-id must be canonical")

    async def _run() -> bool:
        job = await get_ingestion_job(arxiv_id)
        if job is not None:
            return await retry_ingestion_job(arxiv_id)
        paper = await asyncio.to_thread(get_paper, arxiv_id)
        if paper is None:
            return False
        return await enqueue_ingestion_job(
            arxiv_id,
            max(int(paper.get("version") or 1), 1),
            "manual",
            max_attempts=settings.ingest_max_attempts,
        )

    if not asyncio.run(_with_pool(_run)):
        raise click.ClickException(
            "The paper was not found, or its ingestion job is already running."
        )
    click.echo("queued")


@scheduler_group.command("resume-fulltext")
@click.option("--limit", type=click.IntRange(1, 10_000), default=500, show_default=True)
@click.option("--apply", is_flag=True, help="Enqueue recovery work; otherwise dry-run.")
def resume_fulltext_cmd(limit: int, apply: bool) -> None:
    """Resume versions deferred during lean operation, in bounded batches."""
    require_full_runtime("Deferred full-text recovery")
    from scholight.db.queries_deferred_fulltext import resume_deferred_fulltext

    async def run() -> dict[str, int | bool]:
        return await resume_deferred_fulltext(limit=limit, apply=apply)

    click.echo(json.dumps(asyncio.run(_with_pool(run)), sort_keys=True))


@scheduler_group.command("adopt-baseline")
@click.option("--plan", required=True, help="Verified S3 abstract reconciliation plan prefix.")
@click.option("--proof-sha256", required=True)
@click.option("--date", required=True, type=click.DateTime(formats=["%Y-%m-%d"]))
@click.option("--scope-start", required=True, type=click.DateTime(formats=["%Y-%m-%d"]))
@click.option("--scope-end", type=click.DateTime(formats=["%Y-%m-%d"]))
def adopt_baseline_cmd(
    plan: str,
    proof_sha256: str,
    date: dt.datetime,
    scope_start: dt.datetime,
    scope_end: dt.datetime | None,
) -> None:
    """Bind the reviewed destination baseline and seed only its interruption scope."""
    require_full_runtime("Destination adoption")
    from scholight.db.target_adoption import adopt_baseline

    async def run() -> dict[str, Any]:
        return await adopt_baseline(
            plan,
            proof_sha256,
            date=date.date(),
            scope_start=scope_start.date(),
            scope_end=scope_end.date() if scope_end else None,
        )

    click.echo(json.dumps(asyncio.run(_with_pool(run)), default=str, sort_keys=True))
