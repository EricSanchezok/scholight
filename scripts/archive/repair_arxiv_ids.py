#!/usr/bin/env python3
"""Repair short arXiv IDs via canonical padding.

All short IDs belong to 2007-2014 papers (YYMM.NNNN format, 4-digit suffix).
Padding rules:

  Prefix: pad to 4 digits (LEADING zero)  →  801.0001 → 0801.0001
  Suffix: pad to 4 digits (TRAILING zero)  →  1002.49  → 1002.4900
                                            →  1002.342 → 1002.3420

Old-subject IDs (``/``) are skipped.

Output: data/arxiv_id_repair_map.jsonl  — {"old": "...", "new": "..."} per line.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_OUTPUT = _PROJECT_ROOT / "data" / "arxiv_id_repair_map.jsonl"

logger = structlog.get_logger("repair-ids")


def _repair(aid: str) -> str | None:
    """Return repaired arXiv ID, or None if already canonical.

    Prefix: pad to 4 digits (leading zero).
    Suffix: pad to 4 digits for 2007-2014, 5 digits for 2015+ (trailing zero).
    """
    if "/" in aid:
        return None

    parts = aid.split(".")
    if len(parts) != 2:
        return None

    prefix, suffix = parts
    prefix_len, suffix_len = len(prefix), len(suffix)

    # Determine target suffix length from year
    yy = int(prefix[:2]) if prefix[:2].isdigit() else 0
    target_suffix = 5 if yy >= 15 else 4

    # Already canonical?
    if prefix_len == 4 and suffix_len >= target_suffix:
        return None

    new_prefix = prefix.zfill(4)
    new_suffix = suffix.ljust(target_suffix, "0")

    return f"{new_prefix}.{new_suffix}"


def scan() -> None:
    from scholight.store.client import get_client

    client = get_client()

    repairs: list[dict[str, str]] = []
    total = 0
    last_id = ""
    batch = 8000

    while True:
        flt = f"arxiv_id > '{last_id}'" if last_id else "arxiv_id != ''"
        rows = client.query("arxiv_papers", filter=flt, output_fields=["arxiv_id"], limit=batch)
        if not rows:
            break

        for row in rows:
            aid = row["arxiv_id"]
            total += 1
            repaired = _repair(aid)
            if repaired and repaired != aid:
                repairs.append({"old": aid, "new": repaired})
            last_id = aid

        if total % 400_000 == 0:
            logger.info("scanned", count=total)

    _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with _OUTPUT.open("w", encoding="utf-8") as f:
        for r in repairs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    logger.info(
        "done",
        total_papers=total,
        repair_candidates=len(repairs),
        output=str(_OUTPUT),
    )

    # Summary
    print(f"\nTotal: {total}")
    print(f"Repair candidates: {len(repairs)}")
    if repairs:
        print("\nFirst 10:")
        for r in repairs[:10]:
            print(f"  {r['old']:15s} → {r['new']}")


if __name__ == "__main__":
    scan()
