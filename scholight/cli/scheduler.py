"""CLI: ``scholight scheduler`` — daily arxiv pipeline daemons.

Usage::

    scholight scheduler paper-sync     # one-shot metadata sync
    scholight scheduler pdf-daemon     # long-running PDF download daemon
    scholight scheduler md-daemon      # long-running markdown parse daemon
    scholight scheduler chunk-daemon   # long-running chunk ingest daemon
    scholight scheduler status         # pipeline progress report
"""

from __future__ import annotations

import asyncio

import click


@click.group("scheduler")
def scheduler_group() -> None:
    """Daily arXiv pipeline — sync metadata, download PDFs, parse, ingest chunks."""


# ── Paper sync (one-shot) ──────────────────────────────────────────────────────


@scheduler_group.command("paper-sync")
def paper_sync_cmd() -> None:
    """One-shot paper metadata sync — auto-detects start date from database."""
    from scholight.scheduler.arxiv_paper_sync import run_sync

    click.secho("Starting paper sync (OAI-PMH + API fallback)...", fg="cyan")
    try:
        result = asyncio.run(run_sync())
    except KeyboardInterrupt:
        click.secho("Interrupted — safe to re-run.", fg="yellow")
        return
    except Exception:
        click.secho("Sync crashed — check logs/arxiv_sync/arxiv_paper_sync.log", fg="red")
        raise

    if result["days"] == 0:
        click.secho("Already up to date.", fg="green")
    else:
        failed = result["sources"].get("failed", 0)
        parts = [f"OAI: {result['sources']['oai']}"]
        if result["sources"].get("oai_fallback", 0):
            parts.append(f"OAI-fallback: {result['sources']['oai_fallback']}")
        parts.append(f"API: {result['sources']['api']}")
        click.echo(
            f"Synced {result['days']} day(s), {result['papers']} papers ({', '.join(parts)})"
        )
        if failed:
            click.secho(f"  ⚠ {failed} day(s) failed — will retry next cycle.", fg="yellow")
        click.secho("Done.", fg="green")


# ── Long-running daemons ───────────────────────────────────────────────────────


@scheduler_group.command("pdf-daemon")
def pdf_daemon_cmd() -> None:
    """Start PDF download daemon — polls every 5min, downloads PDFs for new papers."""
    from scholight.scheduler.pdf_download import PdfDownloadDaemon

    click.secho("Starting PDF download daemon (polls every 5 min) — Ctrl+C to stop", fg="cyan")
    PdfDownloadDaemon().run()


@scheduler_group.command("md-daemon")
def md_daemon_cmd() -> None:
    """Start markdown parse daemon — polls every 5min, converts PDFs to markdown."""
    from scholight.scheduler.md_parse import MdParseDaemon

    click.secho("Starting markdown parse daemon (polls every 5 min) — Ctrl+C to stop", fg="cyan")
    MdParseDaemon().run()


@scheduler_group.command("chunk-daemon")
def chunk_daemon_cmd() -> None:
    """Start chunk ingest daemon — polls every 5min, chunks + embeds + inserts."""
    from scholight.scheduler.chunk_ingest import ChunkIngestDaemon

    click.secho("Starting chunk ingest daemon (polls every 5 min) — Ctrl+C to stop", fg="cyan")
    ChunkIngestDaemon().run()


# ── Status ──────────────────────────────────────────────────────────────────────


@scheduler_group.command("status")
def status_cmd() -> None:
    """Show pipeline progress — papers pending at each stage."""
    from scholight.store.ingest import count_papers_without

    labels = {
        "has_latex": "without LaTeX:     ",
        "has_pdf": "without PDF:        ",
        "has_markdown": "without markdown:",
        "has_chunks": "without chunks:  ",
    }
    try:
        for flag, label in labels.items():
            count = count_papers_without(flag)
            click.echo(f"  {label} {count:>8,}")
    except Exception:
        click.secho("(unable to query pipeline status — is Zilliz Cloud reachable?)", fg="yellow")
