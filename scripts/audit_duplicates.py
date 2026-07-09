#!/usr/bin/env python3
"""Deep audit — catch any duplicates that may have slipped through.

Three checks:
  1. title-only duplicates (relax abstract constraint to catch near-identical)
  2. remaining short-format IDs that evaded the scanner
  3. potential old→new format overlaps

Output: data/audit_report.txt
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import structlog

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_OUTPUT = _PROJECT_ROOT / "data" / "audit_report.txt"

logger = structlog.get_logger("audit")


def _norm_title(t: str) -> str:
    """Aggressive title normalization: lowercase, collapse whitespace, strip punctuation."""
    s = " ".join(t.strip().lower().split())
    # Strip common trailing punctuation in arXiv titles
    return s.rstrip(".!?,;:\"' ")


def _is_short(aid: str) -> bool:
    """Return True if prefix < 4 or suffix < 4 digits."""
    if "/" in aid:
        return False
    parts = aid.split(".")
    if len(parts) != 2:
        return True
    return len(parts[0]) < 4 or len(parts[1]) < 4


def audit() -> None:
    from compass.store.client import get_client

    client = get_client()
    lines: list[str] = []

    # ── Check 1: title-only duplicates ──────────────────────────────────
    logger.info("check 1 — title-only dup groups")
    title_groups: dict[str, list[str]] = defaultdict(list)
    total = 0
    last_id = ""

    while True:
        flt = f"arxiv_id > '{last_id}'" if last_id else "arxiv_id != ''"
        rows = client.query(
            "arxiv_papers",
            filter=flt,
            output_fields=["arxiv_id", "title"],
            limit=8000,
        )
        if not rows:
            break
        for row in rows:
            t = _norm_title(row["title"])
            if len(t) > 5:
                title_groups[t].append(row["arxiv_id"])
            total += 1
            last_id = row["arxiv_id"]
        if total % 600_000 == 0:
            logger.info("check1 scanned", count=total)

    title_dups = {t: ids for t, ids in title_groups.items() if len(ids) > 1}
    lines.append("=== Check 1: Title-only duplicates ===")
    lines.append(f"Groups: {len(title_dups)}")
    if title_dups:
        lines.append("Top 10:")
        for t, ids in sorted(title_dups.items(), key=lambda x: -len(x[1]))[:10]:
            lines.append(f"  [{len(ids)}] {t[:80]}")
            lines.append(f"    IDs: {sorted(ids)[:5]}")
    else:
        lines.append("None found ✓")

    # ── Check 2: Remaining short IDs ────────────────────────────────────
    logger.info("check 2 — short IDs")
    short_found: list[str] = []
    last_id = ""
    total2 = 0
    while True:
        flt = f"arxiv_id > '{last_id}'" if last_id else "arxiv_id != ''"
        rows = client.query(
            "arxiv_papers",
            filter=flt,
            output_fields=["arxiv_id"],
            limit=8000,
        )
        if not rows:
            break
        for row in rows:
            aid = row["arxiv_id"]
            total2 += 1
            if _is_short(aid):
                short_found.append(aid)
            last_id = aid
        if total2 % 600_000 == 0:
            logger.info("check2 scanned", count=total2)

    lines.append("")
    lines.append("=== Check 2: Remaining short IDs ===")
    lines.append(f"Total papers scanned: {total2}")
    lines.append(f"Short IDs found: {len(short_found)}")
    if short_found:
        lines.append(f"Samples: {short_found[:20]}")
    else:
        lines.append("None found ✓")

    # ── Check 3: Old-subject → new-format potential overlaps ────────────
    logger.info("check 3 — old/new overlaps")
    # Sample old-subject papers, check if same title exists in new format
    old_sample: list[dict] = []
    last_id = ""
    while len(old_sample) < 5000:
        flt = f"arxiv_id > '{last_id}'" if last_id else "arxiv_id != ''"
        rows = client.query(
            "arxiv_papers",
            filter=flt,
            output_fields=["arxiv_id", "title"],
            limit=8000,
        )
        if not rows:
            break
        for row in rows:
            aid = row["arxiv_id"]
            if "/" in aid:
                old_sample.append({"arxiv_id": aid, "title": _norm_title(row["title"])})
                if len(old_sample) >= 5000:
                    break
            last_id = aid

    # Build title index from new-format papers (sample first 50k)
    new_titles: dict[str, str] = {}
    last_id = ""
    while len(new_titles) < 50000:
        flt = f"arxiv_id > '{last_id}'" if last_id else "arxiv_id != ''"
        rows = client.query(
            "arxiv_papers",
            filter=flt,
            output_fields=["arxiv_id", "title"],
            limit=8000,
        )
        if not rows:
            break
        for row in rows:
            aid = row["arxiv_id"]
            if "/" not in aid:
                new_titles[_norm_title(row["title"])] = aid
            last_id = aid

    overlaps = []
    for p in old_sample:
        if p["title"] in new_titles:
            overlaps.append(
                f"  old: {p['arxiv_id']}  →  new: {new_titles[p['title']]}  ({p['title'][:60]})"
            )

    lines.append("")
    lines.append("=== Check 3: Old→New format overlaps (sampled) ===")
    lines.append(f"Old-subject sampled: {len(old_sample)}")
    lines.append(f"New-format indexed:  {len(new_titles)}")
    lines.append(f"Overlaps found: {len(overlaps)}")
    if overlaps:
        lines.append("Matches:")
        for o in overlaps[:20]:
            lines.append(o)
    else:
        lines.append("None found ✓")

    # ── Write report ────────────────────────────────────────────────────
    _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT.write_text("\n".join(lines))
    logger.info("done", output=str(_OUTPUT))


if __name__ == "__main__":
    audit()
