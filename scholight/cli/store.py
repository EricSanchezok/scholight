"""CLI commands for Zilliz administration and Scholight PostgreSQL migrations."""

from __future__ import annotations

import click

from scholight.cli.archive import (
    export_cmd,
    initialize_cmd,
    restore_cmd,
    verify_cmd,
    verify_restored_cmd,
)
from scholight.cli.reconcile import reconcile_cmd
from scholight.config import active_collections
from scholight.store.client import connect, is_connected
from scholight.store.schema import create_collections, create_indexes


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
    """Initialize collections required by the selected runtime profile."""
    client = connect()
    click.echo("Connected to Milvus ✓")

    create_collections(client)
    click.echo("Collections created (or already exist) ✓")

    create_indexes(client)
    click.echo("Indexes built ✓")

    for name in active_collections():
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

    for name in active_collections():
        if client.has_collection(name):
            stats = client.get_collection_stats(name)
            total = f"{stats.get('row_count', 0):>8,d}"
            indexes = client.list_indexes(name)
            click.echo(f"  {name:>17s}: {total} rows, {len(indexes)} indexes")
        else:
            click.echo(f"  {name}: NOT CREATED")


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
            click.echo(
                "Could not estimate collection size; continuing with the requested deep scan.",
                err=True,
            )

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


for command in (export_cmd, initialize_cmd, restore_cmd, verify_cmd, verify_restored_cmd):
    store_group.add_command(command)


store_group.add_command(reconcile_cmd)
