#!/usr/bin/env python3
"""Filter duplicate paper groups — keep longest IDs, delete shorter ones.

Reads data/duplicate_papers.txt, applies::

    1. Find the *longest* ID(s) in each group (by prefix_digits + suffix_digits)
    2. Delete all IDs shorter than the longest
    3. If multiple IDs share the same longest length, keep ALL of them
       (they may be distinct papers with identical title+authors+abstract)

Writes data/duplicates_to_delete.txt — one arxiv_id per line.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_INPUT = _PROJECT_ROOT / "data" / "duplicate_papers.txt"
_OUTPUT = _PROJECT_ROOT / "data" / "duplicates_to_delete.txt"

logger = structlog.get_logger("filter-duplicates")


def _rank(aid: str) -> tuple[int, int]:
    """Return (prefix_digits, suffix_digits).  Higher = longer ID."""
    if "/" in aid:
        return (0, 0)
    parts = aid.split(".")
    if len(parts) != 2:
        return (-1, -1)
    return (len(parts[0]), len(parts[1]))


def filter_groups() -> None:
    groups: list[list[str]] = []
    with _INPUT.open("r", encoding="utf-8") as f:
        for line in f:
            groups.append(json.loads(line.strip()))

    logger.info("loaded duplicate groups", count=len(groups))

    to_delete: list[str] = []

    for group in groups:
        if len(group) < 2:
            continue

        # Rank every ID by (prefix_len, suffix_len)
        ranked = [(_rank(aid), aid) for aid in group]
        max_rank = max(r for r, _ in ranked)

        for r, aid in ranked:
            if r < max_rank:
                to_delete.append(aid)
            # r == max_rank → keep

    to_delete.sort()
    with _OUTPUT.open("w", encoding="utf-8") as f:
        for aid in to_delete:
            f.write(aid + "\n")

    kept = sum(1 for g in groups for _ in g) - len(to_delete)
    logger.info(
        "done",
        duplicate_groups=len(groups),
        to_delete=len(to_delete),
        to_keep=kept,
        output=str(_OUTPUT),
    )


if __name__ == "__main__":
    filter_groups()
