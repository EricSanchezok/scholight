"""CLI commands for Zilliz administration and Scholight PostgreSQL migrations."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import click

from scholight.store.client import connect, is_connected
from scholight.store.export import export_collection_to_path, restore_collection_from_path
from scholight.store.schema import create_collections, create_indexes

_COLLECTIONS = ("arxiv_papers", "arxiv_chunks")


@click.group("store")
def store_group() -> None:
    """Manage Zilliz storage and Scholight PostgreSQL migrations."""


@store_group.command()
def migrate() -> None:
    """Validate auth and apply Scholight-owned PostgreSQL migrations."""
    import asyncio

    from scholight.db.client import close_pool, create_pool
    from scholight.db.migrate import run_migrations

    async def migrate_postgres() -> None:
        try:
            pool = await create_pool()
            await run_migrations(pool)
        finally:
            await close_pool()

    asyncio.run(migrate_postgres())
    click.echo("Scholight PostgreSQL migrations applied.")


@store_group.command()
def init() -> None:
    """Create arxiv_papers + arxiv_chunks collections, build indexes, load."""
    client = connect()
    click.echo("Connected to Milvus ✓")

    create_collections(client)
    click.echo("Collections created (or already exist) ✓")

    create_indexes(client)
    click.echo("Indexes built ✓")

    for name in _COLLECTIONS:
        try:
            client.load_collection(name, timeout=3600)
            click.echo(f"Collection '{name}' loaded into memory ✓")
        except Exception as exc:
            click.echo(f"Collection '{name}' load failed: {exc}", err=True)
            raise

    click.echo("\nAll done.  Collections ready for ingestion + search.")


@store_group.command()
def status() -> None:
    """Check Milvus connection status and per-collection row counts."""
    if not is_connected():
        click.echo("Milvus: NOT CONNECTED")
        return

    client = connect()
    click.echo("Milvus: connected ✓\n")

    for name in _COLLECTIONS:
        if client.has_collection(name):
            stats = client.get_collection_stats(name)
            total = f"{stats.get('row_count', 0):>8,d}"
            indexes = client.list_indexes(name)
            click.echo(f"  {name:>17s}: {total} rows, {len(indexes)} indexes")
        else:
            click.echo(f"  {name}: NOT CREATED")


@store_group.command()
@click.option(
    "--output-dir",
    "-o",
    type=click.Path(file_okay=False),
    help="Target directory (default: data_root/backups/logical/<timestamp>/)",
)
def backup(output_dir: str | None) -> None:
    """Logical export: cursor-scan arxiv_papers + arxiv_chunks → JSONL shards.

    Each collection gets its own subdirectory:
    ``<output>/arxiv_papers/shard_*.jsonl.gz`` and
    ``<output>/arxiv_chunks/shard_*.jsonl.gz``.

    Runs online — no Milvus downtime needed.
    Default target: ``{data_root}/backups/logical/YYYYMMDD_hhmmss/``.
    """
    from scholight.storage import storage

    root = (
        Path(output_dir)
        if output_dir
        else Path(storage.backup_dir("logical") / datetime.now().strftime("%Y%m%d_%H%M%S"))
    )

    client = connect()
    grand_total = 0
    for name in _COLLECTIONS:
        if not client.has_collection(name):
            click.echo(f"  {name}: skipping — collection not found")
            continue
        dest = root / name
        total = export_collection_to_path(client, name, dest)
        grand_total += total
        click.echo(f"  {name}: {total:,} rows → {dest}")
    click.echo(f"\nExported {grand_total:,} rows → {root}")


@store_group.command()
@click.option(
    "--deep",
    is_flag=True,
    help="Full cursor-scan analysis (slow on large collections).",
)
@click.option(
    "-d",
    "--dim",
    "dims",
    multiple=True,
    type=click.Choice(
        [
            "connection",
            "collections",
            "indexes",
            "segments",
            "data_stats",
            "resources",
            "vectors",
            "consistency",
        ],
        case_sensitive=False,
    ),
    help="Run only the specified health check dimension(s). Repeatable.",
)
@click.option(
    "--fix",
    is_flag=True,
    help="Auto-fix recoverable issues (load, flush, compact).",
)
@click.option(
    "--output",
    "-o",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format.",
)
@click.option(
    "--output-file",
    type=click.Path(dir_okay=False, writable=True),
    help="Write report to file (JSON only).",
)
def health(
    deep: bool,
    dims: tuple[str, ...],
    fix: bool,
    output: str,
    output_file: str | None,
) -> None:
    """Database health check — 7-layer progressive diagnosis.

    L0  Connection   → Milvus reachability, server version
    L1  Collections  → Existence, schema, load state
    L2  Indexes      → Per-index state, pending rows
    L3  Segments     → Loaded/persistent, growing vs sealed, memory
    L4  Data Stats   → Row count, year distribution, field completeness
    L5  Resources    → Pipeline flag coverage
    L6  Vectors      → Zero-vector ratio
    L7  Consistency  → Papers ↔ Chunks cross-check

    Default (quick) mode runs API-level checks only and completes in <5s.
    Use --deep for full cursor-scan analysis (year/field/vector stats).
    Use --fix to auto-load collections, flush, and trigger compaction.

    \b
    Examples:
        scholight store health                    # quick check
        scholight store health --deep             # full analysis
        scholight store health -d indexes -d vectors  # specific layers only
        scholight store health --fix              # auto-fix recoverable issues
        scholight store health -o json            # machine-readable output
    """
    from scholight.store.health import run_health_check

    dim_list = list(dims) if dims else None

    if deep:
        client = connect()
        try:
            stats = client.get_collection_stats("arxiv_papers")
            row_count = stats.get("row_count", 0)
            if row_count > 100_000:
                click.echo(
                    f"\n⚠  Deep mode will scan ~{row_count:,} rows (cursor-based full traversal).\n"
                    f"   This may take several minutes and incur Zilliz Cloud CU costs.\n"
                    f"   Consider using quick mode or filtering by dimension (-d) instead.\n",
                    err=True,
                )
                click.confirm("Continue with deep scan?", abort=True)
        except Exception:
            pass  # if stats fails, proceed anyway

    click.echo("Running health check…", err=True)
    report = run_health_check(deep=deep, dims=dim_list, fix=fix)

    if output == "json":
        import json as _json

        content = _json.dumps(report.to_dict(), indent=2, ensure_ascii=False, default=str)
        if output_file:
            with open(output_file, "w") as f:
                f.write(content)
            click.echo(f"Report saved to {output_file}")
        else:
            click.echo(content)
    else:
        click.echo(report.print())

    if not report.healthy:
        raise SystemExit(1)


@store_group.command()
@click.argument("input_dir", type=click.Path(file_okay=False, exists=True))
@click.option("--batch-size", type=int, default=1000, help="Rows per upsert batch.")
def restore(input_dir: str, batch_size: int) -> None:
    """Restore a collection from a logical backup subdirectory.

    Reads ``shard_*.jsonl.gz`` files from *input_dir* (the collection-specific
    subdirectory like ``<backup>/arxiv_papers/`` or ``<backup>/arxiv_chunks/``)
    and upserts into Milvus.

    Example: ``scholight store restore backups/logical/20260530/arxiv_papers``
    """
    total = restore_collection_from_path(connect(), "arxiv_papers", Path(input_dir), batch_size)
    click.echo(f"Restored {total:,} rows.")
