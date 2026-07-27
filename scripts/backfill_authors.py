#!/usr/bin/env python3
"""Backfill missing paper authors from arXiv into Zilliz Cloud.

The script is idempotent and dry-run by default. It only scans rows whose
``authors`` array is empty, fetches authoritative metadata from arXiv in
rate-limited batches, and uses partial updates that touch only ``authors``.

Examples:
    uv run python scripts/backfill_authors.py
    uv run python scripts/backfill_authors.py --limit 20
    uv run python scripts/backfill_authors.py --apply --limit 100
    uv run python scripts/backfill_authors.py --apply --limit 0
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from scholight.logging import configure_logging
from scholight.sources.arxiv import API_DELAY_SECONDS, fetch_papers_by_ids
from scholight.store.client import QUERY_CONSISTENCY, escape_sql, get_client
from scholight.store.ingestion import write_metadata_papers

logger = structlog.get_logger("author_backfill")

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_FAILURE_LOG = _PROJECT_ROOT / "data" / "author_backfill_failures.jsonl"
_SCAN_BATCH_SIZE = 1000

Paper = dict[str, Any]
Fetcher = Callable[[list[str]], Awaitable[list[Paper]]]
Writer = Callable[[list[Paper]], object]
Sleeper = Callable[[float], Awaitable[None]]


@dataclass(slots=True)
class BackfillStats:
    scanned: int = 0
    recoverable: int = 0
    updated: int = 0
    batches: int = 0
    unresolved_ids: list[str] = field(default_factory=list)


def collect_missing_author_ids(
    client: Any,
    *,
    limit: int,
    after: str = "",
) -> list[str]:
    """Collect a stable worklist before any rows are changed."""
    if limit < 0:
        raise ValueError("limit must be zero or greater")
    base_filter = "array_length(authors) == 0"
    filter_expression = (
        f"{base_filter} and arxiv_id > '{escape_sql(after)}'" if after else base_filter
    )
    iterator = client.query_iterator(
        "arxiv_papers",
        batch_size=_SCAN_BATCH_SIZE,
        limit=limit or -1,
        filter=filter_expression,
        output_fields=["arxiv_id"],
        consistency_level=QUERY_CONSISTENCY,
    )
    ids: list[str] = []
    try:
        while True:
            rows = iterator.next()
            if not rows:
                break
            ids.extend(
                arxiv_id
                for row in rows
                if isinstance((arxiv_id := row.get("arxiv_id")), str) and arxiv_id
            )
    finally:
        iterator.close()
    return ids


async def backfill_ids(
    arxiv_ids: list[str],
    *,
    apply: bool,
    batch_size: int,
    delay: float,
    fetcher: Fetcher = fetch_papers_by_ids,
    writer: Writer = write_metadata_papers,
    sleeper: Sleeper = asyncio.sleep,
) -> BackfillStats:
    """Resolve and optionally write authors for an explicit paper worklist."""
    if not 1 <= batch_size <= 500:
        raise ValueError("batch_size must be between 1 and 500")
    if delay < 0:
        raise ValueError("delay must be zero or greater")

    stats = BackfillStats(scanned=len(arxiv_ids))
    for offset in range(0, len(arxiv_ids), batch_size):
        batch = arxiv_ids[offset : offset + batch_size]
        papers = await fetcher(batch)
        authors_by_id = {
            str(paper["arxiv_id"]): list(paper.get("authors") or [])
            for paper in papers
            if paper.get("arxiv_id") and paper.get("authors")
        }
        updates = [
            {
                "arxiv_id": arxiv_id,
                "authors": authors_by_id[arxiv_id],
                "_metadata_fields": {"authors"},
            }
            for arxiv_id in batch
            if arxiv_id in authors_by_id
        ]
        unresolved = [arxiv_id for arxiv_id in batch if arxiv_id not in authors_by_id]

        stats.batches += 1
        stats.recoverable += len(updates)
        stats.unresolved_ids.extend(unresolved)
        if apply and updates:
            writer(updates)
            stats.updated += len(updates)

        if stats.batches == 1 or stats.batches % 10 == 0 or offset + len(batch) >= len(arxiv_ids):
            logger.info(
                "author backfill progress",
                mode="apply" if apply else "dry-run",
                processed=min(offset + len(batch), len(arxiv_ids)),
                total=len(arxiv_ids),
                recoverable=stats.recoverable,
                updated=stats.updated,
                unresolved=len(stats.unresolved_ids),
            )
        if offset + len(batch) < len(arxiv_ids) and delay:
            await sleeper(delay)
    return stats


def _write_failure_log(path: Path, ids: list[str]) -> None:
    if not ids:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for arxiv_id in ids:
            handle.write(json.dumps({"arxiv_id": arxiv_id, "reason": "authors unavailable"}))
            handle.write("\n")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write recovered authors to Zilliz. Without this flag, no data is changed.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum empty-author rows to inspect; use 0 for all rows (default: 100).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=200,
        help="arXiv IDs fetched per request, from 1 to 500 (default: 200).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=API_DELAY_SECONDS,
        help=f"Seconds between successful arXiv requests (default: {API_DELAY_SECONDS}).",
    )
    parser.add_argument(
        "--after",
        default="",
        help="Only inspect primary keys lexically after this canonical arXiv ID.",
    )
    parser.add_argument(
        "--failure-log",
        type=Path,
        default=_DEFAULT_FAILURE_LOG,
        help=f"Unresolved ID output path (default: {_DEFAULT_FAILURE_LOG}).",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.limit < 0:
        raise SystemExit("--limit must be zero or greater")
    if not 1 <= args.batch_size <= 500:
        raise SystemExit("--batch-size must be between 1 and 500")
    if args.delay < API_DELAY_SECONDS:
        raise SystemExit(f"--delay must be at least {API_DELAY_SECONDS} seconds")

    configure_logging(log_level="INFO", use_json=False)
    mode = "apply" if args.apply else "dry-run"
    logger.info(
        "collecting empty-author worklist",
        mode=mode,
        limit=args.limit or "all",
        after=args.after or None,
    )
    ids = collect_missing_author_ids(get_client(), limit=args.limit, after=args.after)
    if not ids:
        logger.info("no empty-author papers found")
        return

    stats = asyncio.run(
        backfill_ids(
            ids,
            apply=args.apply,
            batch_size=args.batch_size,
            delay=args.delay,
        )
    )
    _write_failure_log(args.failure_log, stats.unresolved_ids)
    logger.info(
        "author backfill complete",
        mode=mode,
        scanned=stats.scanned,
        recoverable=stats.recoverable,
        updated=stats.updated,
        unresolved=len(stats.unresolved_ids),
        failure_log=str(args.failure_log) if stats.unresolved_ids else None,
    )
    if not args.apply and stats.recoverable:
        logger.info("dry-run only; rerun with --apply to write recovered authors")


if __name__ == "__main__":
    main()
