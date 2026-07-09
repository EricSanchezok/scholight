#!/usr/bin/env python3
"""Scan arxiv_papers for duplicate papers (same title + same authors).

Output: data/duplicate_papers.txt — one line per duplicate group, format::

    [arxiv_id1, arxiv_id2, ...]

Groups with only one member are skipped (no duplicate found).
Sorted by arxiv_id so the output is deterministic and diffable.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import structlog

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_OUTPUT = _PROJECT_ROOT / "data" / "duplicate_papers.txt"

logger = structlog.get_logger("scan-duplicates")


def _key(paper: dict) -> str:
    """Normalised dedup key: title + sorted authors + abstract.

    Collapses internal whitespace so ``"A   B"`` == ``"A B"``.
    Abstract comparison catches same-title-same-author but different-content
    duplicates (e.g. a resubmission with different content).
    """
    title = " ".join(paper.get("title", "").strip().lower().split())
    authors = tuple(" ".join(str(a).strip().lower().split()) for a in paper.get("authors", []))
    abstract = " ".join(paper.get("abstract", "").strip().lower().split())
    return f"{title}|||{'|'.join(sorted(authors))}|||{abstract[:512]}"


def scan() -> None:
    from scholight.store.client import get_client

    client = get_client()

    # Cursor-scan: arxiv_id > last_id with strict ordering.
    # Batch size chosen to stay under Milvus 16384 query window.
    groups: dict[str, list[str]] = defaultdict(list)
    total = 0
    last_id = ""
    batch = 8000  # (limit) must be ≤ 16384

    while True:
        flt = f"arxiv_id > '{last_id}'" if last_id else "arxiv_id != ''"
        rows = client.query(
            "arxiv_papers",
            filter=flt,
            output_fields=["arxiv_id", "title", "authors", "abstract"],
            limit=batch,
        )
        if not rows:
            break

        for row in rows:
            aid = row["arxiv_id"]
            groups[_key(row)].append(aid)
            last_id = aid
            total += 1

        if total % 200_000 == 0:
            logger.info("scanned", count=total, groups=len(groups))

    # Filter out singletons
    dups: list[list[str]] = [sorted(ids) for ids in groups.values() if len(ids) > 1]
    dups.sort(key=lambda g: g[0])  # stable across runs

    _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with _OUTPUT.open("w", encoding="utf-8") as f:
        for group in dups:
            f.write(json.dumps(group, ensure_ascii=False) + "\n")

    logger.info(
        "done",
        total_papers=total,
        duplicate_groups=len(dups),
        total_duplicate_papers=sum(len(g) for g in dups),
        output=str(_OUTPUT),
    )


if __name__ == "__main__":
    scan()
