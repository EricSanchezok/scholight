"""Scholight search CLI — ``scholight search`` command with rich terminal diagnostics."""

from __future__ import annotations

import asyncio
import json as _json
import sys
import textwrap
import traceback

import click
import httpx
import structlog

from scholight.logging import configure_logging
from scholight.models.search import SearchHit, SearchRequest, SearchResult
from scholight.search.engine import SearchEngine
from scholight.storage import storage

logger = structlog.get_logger(__name__)

# ══════════════════════════════════════════════════════════════════════
# Shared CLI helpers
# ══════════════════════════════════════════════════════════════════════

_SEPARATOR = "─" * 72
_DOUBLE_SEP = "═" * 72
_INDENT = "     "
_LABEL_W = 20
_LABEL_COLOR = "bright_black"  # key label text color — subdued, distinct from values


def _label(key: str) -> str:
    """Right-justified label key for field/value pairs."""
    return click.style(f"{key + ':':>{_LABEL_W}s}", fg=_LABEL_COLOR)


def _emit(key: str, value: str) -> None:
    """Emit a labelled field line, suppressing empty/None values.

    The string ``"0"`` is intentionally *not* suppressed — fields like
    ``version`` may legitimately be zero.
    """
    if not value:
        return
    click.echo(f"{_INDENT}{_label(key)}  {value}")


def _emit_wrapped(key: str, text: str) -> None:
    """Emit a field value with line wrapping at ~70 chars."""
    if not text:
        return
    first = f"{_INDENT}{_label(key)}  "
    cont = f"{_INDENT}{' ' * (_LABEL_W + 2)}"
    lines = textwrap.wrap(text, width=68)
    for i, line in enumerate(lines):
        click.echo(f"{first if i == 0 else cont}{line}")


def _emit_wrapped_styled(key: str, text: str, *, fg: str) -> None:
    """Emit a wrapped field with Click text styling applied."""
    if not text:
        return
    first = f"{_INDENT}{_label(key)}  "
    cont = f"{_INDENT}{' ' * (_LABEL_W + 2)}"
    lines = textwrap.wrap(text, width=68)
    for i, line in enumerate(lines):
        click.echo(f"{first if i == 0 else cont}{click.style(line, fg=fg)}")


def _score_bar(score: float, width: int = 40) -> str:
    """Visual score bar: ``[████████████████████████████░░░░] 0.9234``"""
    filled = min(int(score * width), width)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {score:.4f}"


# ══════════════════════════════════════════════════════════════════════
# Search command
# ══════════════════════════════════════════════════════════════════════


@click.command("search")
@click.option("-q", "--query", required=True, help="Search query text")
@click.option("-k", "--top-k", type=int, default=10, show_default=True, help="Number of results")
@click.option(
    "-l",
    "--level",
    type=click.IntRange(1, 3),
    default=1,
    show_default=True,
    help="Search depth: 1=paper, 2=+chunks, 3=+figures/tables",
)
# ── Pipeline stage toggles ──
@click.option(
    "--strategy",
    type=click.Choice(["fast", "hybrid_fusion"]),
    default=None,
    help="Named search strategy (overrides --fusion when set)",
)
@click.option("--fusion", is_flag=True, help="Enable multi-signal score fusion (Phase 3)")
# ── Filters ──
@click.option("-c", "--categories", multiple=True, help="arXiv category filter (repeatable)")
@click.option("-a", "--authors", multiple=True, help="Author name filter (repeatable)")
@click.option("-i", "--arxiv-ids", multiple=True, help="Exact arXiv ID filter (repeatable)")
@click.option("--date-from", help="Earliest date (YYYY-MM-DD)")
@click.option("--date-to", help="Latest date (YYYY-MM-DD)")
@click.option("--json", "as_json", is_flag=True, help="JSON output mode")
def search_cmd(
    query: str,
    top_k: int,
    level: int,
    strategy: str | None,
    fusion: bool,
    categories: tuple[str, ...],
    authors: tuple[str, ...],
    arxiv_ids: tuple[str, ...],
    date_from: str | None,
    date_to: str | None,
    as_json: bool,
) -> None:
    """Search papers with detailed diagnostics.

    Use --fusion to enable multi-signal score fusion re-ranking.
    """
    _log_path = storage.log_path("search", "cli.log")
    configure_logging(
        log_level="INFO", use_json=False, file_handler=(str(_log_path), 50_000_000, 3)
    )
    structlog.get_logger("httpx").setLevel("ERROR")
    structlog.get_logger("pymilvus").setLevel("ERROR")

    request = SearchRequest(
        query=query,
        top_k=top_k,
        level=level,
        strategy=strategy,
        enable_fusion=fusion,
        date_from=date_from or None,
        date_to=date_to or None,
        categories=list(categories) if categories else None,
        authors=list(authors) if authors else None,
        arxiv_ids=list(arxiv_ids) if arxiv_ids else None,
    )

    async def _run() -> SearchResult:
        engine = SearchEngine()
        return await engine.search(request)

    try:
        result = asyncio.run(_run())
    except NotImplementedError as exc:
        click.secho(f"Error: {exc}", fg="red", err=True)
        sys.exit(1)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if hasattr(exc, "response") else "unknown"
        click.secho(f"Embedding API error (HTTP {status}): {exc}", fg="red", err=True)
        sys.exit(1)
    except Exception as exc:
        click.secho(f"Error: {exc}", fg="red", err=True)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

    if as_json:
        _print_json(result)
    else:
        _print_report(result, request)


# ══════════════════════════════════════════════════════════════════════
# Output formatters
# ══════════════════════════════════════════════════════════════════════


def _print_json(result: SearchResult) -> None:
    """Compact JSON output via Pydantic model dump."""
    payload = result.model_dump(mode="json")
    click.echo(_json.dumps(payload, indent=2))


def _build_filter_summary(request: SearchRequest) -> str:
    """Summarise active filters for the report header."""
    parts: list[str] = []
    if request.categories:
        parts.append(f"categories={','.join(request.categories)}")
    if request.authors:
        parts.append(f"authors={','.join(request.authors)}")
    if request.arxiv_ids:
        parts.append(f"arxiv_ids={','.join(request.arxiv_ids)}")
    if request.date_from:
        parts.append(f"date_from={request.date_from}")
    if request.date_to:
        parts.append(f"date_to={request.date_to}")
    return "  ".join(parts) if parts else "(none)"


def _print_report(result: SearchResult, request: SearchRequest) -> None:
    """Rich terminal report — multi-section diagnostics + paper cards."""
    stats = result.stats
    filter_str = _build_filter_summary(request)

    # ── Header ──────────────────────────────────────────────────
    click.echo()
    click.secho(_DOUBLE_SEP, fg=_LABEL_COLOR)
    level_str = f"[level={result.level}]"
    click.echo(f"  Scholight Search  {click.style(level_str, fg='cyan')}")
    click.echo(f"  Query:  {result.query}")
    click.echo(f"  Top-K:  {request.top_k}")
    click.echo(f"  Filters: {filter_str}")
    click.secho(_DOUBLE_SEP, fg=_LABEL_COLOR)
    click.echo()

    # ── Algorithm ────────────────────────────────────────────────
    if stats:
        click.secho("  📋 Algorithm", fg="yellow", bold=True)
        level_desc: dict[int, str] = {
            1: "paper-only (hybrid dense+sparse)",
            2: "paper + chunk (BM25→Dense + RRF fusion)",
            3: "paper + chunk + surface/table (not yet implemented)",
        }
        _emit("Search level", level_desc.get(stats.level, str(stats.level)))
        _emit(
            "Embedding model",
            f"{stats.embedding_model} (dim={stats.embedding_dim})",
        )
        # Derive retrieval mode from phase metadata
        retrieval_desc = "COSINE (AUTOINDEX)"
        for p in stats.phases:
            if p.phase == "paper_search" and p.metadata.get("mode") == "hybrid":
                retrieval_desc = "WeightedRanker: dense + abstract_bm25"
                break
        _emit("Retrieval", retrieval_desc)
        # Stage toggles
        stages: list[str] = []
        for p in stats.phases:
            if p.phase == "score_fusion" and p.metadata.get("enabled"):
                stages.append("fusion")
        _emit("Active stages", ", ".join(stages) if stages else "(none)")
        click.echo()

    # ── Phase Timing ─────────────────────────────────────────────
    if stats and stats.phases:
        click.secho("  ⏱  Phase Timing", fg="yellow", bold=True)
        click.echo(f"  {'Phase':<20} {'Duration':>10}")
        click.echo(f"  {'─' * 20} {'─' * 10}")
        for p in stats.phases:
            meta_str = ""
            if p.metadata:
                items = [f"{k}={v}" for k, v in p.metadata.items()]
                meta_str = f"  ({', '.join(items)})"
            click.echo(f"  {p.phase:<20} {p.duration_ms:>8.1f}ms{meta_str}")
        click.echo(
            f"  {click.style('─' * 20, fg=_LABEL_COLOR)} {click.style('─' * 10, fg=_LABEL_COLOR)}"
        )
        click.echo(f"  {'TOTAL':<20} {result.total_ms:>8.1f}ms")
        click.echo()

    # ── Results waterfall ─────────────────────────────────────────
    if stats:
        click.secho("  🌊 Results", fg="yellow", bold=True)
        click.echo(f"  Paper candidates:  {stats.paper_candidates:>10d}")
        click.echo(f"  Returned:          {len(result.hits):>10d}")
        click.echo()

    # ── Papers ───────────────────────────────────────────────────
    if result.hits:
        click.secho("  📄 Papers", fg="yellow", bold=True)
        click.echo()
        for i, hit in enumerate(result.hits):
            _render_hit(i + 1, hit)

    # ── Store ────────────────────────────────────────────────────
    tp = result.total_papers or 0
    tc = result.total_chunks or 0

    click.secho("  🏪 Store", fg="yellow", bold=True)
    click.echo(f"        arxiv_papers:  {tp:>6,d} papers (loaded)")
    click.echo(f"        arxiv_chunks:  {tc:>6,d} chunks (loaded)")

    click.echo()
    click.echo(f"  Total: {result.total_ms:.1f}ms  |  {len(result.hits)} hits")
    click.echo()


# ── Per-hit rendering ────────────────────────────────────────────────


def _render_hit(rank: int, hit: SearchHit) -> None:
    """Render one SearchHit as a labelled card."""
    # Header line
    idx = click.style(f"# {rank}", fg="cyan", bold=True)
    bar = _score_bar(hit.score)
    click.echo(f"  {idx}  score={click.style(bar, fg='green')}")
    click.secho(f"  {_SEPARATOR}", fg=_LABEL_COLOR)

    # Metadata block — only emit non-empty fields
    _emit("arxiv_id", hit.arxiv_id)
    _emit_wrapped("title", hit.title)
    if hit.authors:
        _emit_wrapped_styled("authors", ", ".join(hit.authors), fg="magenta")
    if hit.abstract:
        _emit_wrapped("abstract", hit.abstract)
    _emit("created", hit.created)
    _emit("updated", hit.updated)
    _emit("version", str(hit.version))
    if hit.updated_history:
        _emit("updated_history", ", ".join(hit.updated_history))
    _emit("license", hit.license)
    _emit("comments", hit.comments)
    if hit.doi:
        click.echo(
            f"{_INDENT}{_label('doi')}  {click.style(f'https://doi.org/{hit.doi}', fg='blue')}"
        )
    _emit("journal_ref", hit.journal_ref)
    _emit("acm_class", hit.acm_class)
    if hit.categories:
        click.echo(
            f"{_INDENT}{_label('categories')}  "
            f"{click.style(', '.join(hit.categories), fg='yellow')}"
        )
    click.echo(f"{_INDENT}{_label('arxiv_url')}  {click.style(hit.url, fg='blue')}")

    click.secho(f"  {_DOUBLE_SEP}", fg=_LABEL_COLOR)
    click.echo()


__all__ = ["search_cmd"]
