#!/usr/bin/env python3
"""Count surviving short-format arXiv IDs after dedup delete.

Output: data/short_id_survivors.txt  — format distribution + samples.
"""

from __future__ import annotations

from pathlib import Path

import structlog

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_OUTPUT = _PROJECT_ROOT / "data" / "short_id_survivors.txt"

logger = structlog.get_logger("stat-short-ids")


def _classify(aid: str) -> str:
    """Bucket: '3d-prefix', '3d-suffix', 'old-subject', 'normal', etc."""
    if "/" in aid:
        return "old-subject"
    parts = aid.split(".")
    if len(parts) != 2:
        return "other"
    pl, sl = len(parts[0]), len(parts[1])
    tags = []
    if pl <= 3:
        tags.append(f"{pl}d-prefix")
    if sl <= 3:
        tags.append(f"{sl}d-suffix")
    return "+".join(tags) if tags else "normal"


def scan() -> None:
    from compass.store.client import get_client

    client = get_client()

    counts: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    total = 0
    last_id = ""
    batch = 8000
    max_samples = 30

    while True:
        flt = f"arxiv_id > '{last_id}'" if last_id else "arxiv_id != ''"
        rows = client.query("arxiv_papers", filter=flt, output_fields=["arxiv_id"], limit=batch)
        if not rows:
            break

        for row in rows:
            aid = row["arxiv_id"]
            total += 1
            label = _classify(aid)
            counts[label] = counts.get(label, 0) + 1
            if label != "normal" and label != "old-subject":
                samples.setdefault(label, [])
                if len(samples[label]) < max_samples:
                    samples[label].append(aid)
            last_id = aid

        if total % 400_000 == 0:
            logger.info("scanned", count=total)

    lines = [f"total papers: {total}", ""]
    lines.append(f"{'format':30s} {'count':>10s}")
    lines.append("-" * 42)
    for label in sorted(counts, key=lambda k: -counts[k]):
        lines.append(f"{label:30s} {counts[label]:10d}")

    # Detailed breakdown
    has_short = any("prefix" in k or "suffix" in k for k in samples)
    if has_short:
        lines.append("")
        lines.append("Samples (non-normal, non-subject):")
        for label in sorted(samples):
            lines.append(f"  [{label}] ({len(samples[label])}):")
            for aid in samples[label][:10]:
                lines.append(f"    {aid}")
    else:
        lines.append("")
        lines.append("No short prefix/suffix survivors — clean ✓")

    _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT.write_text("\n".join(lines))
    logger.info("done", output=str(_OUTPUT))


if __name__ == "__main__":
    scan()
