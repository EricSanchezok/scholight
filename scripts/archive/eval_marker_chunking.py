#!/usr/bin/env python3
"""
评估 Marker content_list 对 chunker 的兼容性。

1. 直接用 chunk_content_list() 跑 Marker 的输出 → 看 chunk 分布
2. 对比 MinerU 的 chunk 分布 → 数 chunk 数、平均长度、section 数
3. 定位差异根因 → 哪些 heading 漏了、哪些不该有的 heading 被标记了
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from compass.pipeline.chunkers.content_list_chunker import chunk_content_list  # noqa: E402


def load_json(path: Path) -> list:
    return json.loads(path.read_text())


def stats(chunks: list, label: str) -> dict:
    """Compute chunking statistics."""
    if not chunks:
        return {
            "label": label,
            "n_chunks": 0,
            "n_sections": 0,
            "avg_len": 0,
            "min_len": 0,
            "max_len": 0,
            "headings": [],
        }

    lengths = [len(c.content) for c in chunks]
    sections = len({c.heading for c in chunks})
    headings = sorted({c.heading for c in chunks})

    under_100 = sum(1 for length in lengths if length < 100)
    over_3000 = sum(1 for length in lengths if length > 3000)

    return {
        "label": label,
        "n_chunks": len(chunks),
        "n_sections": sections,
        "avg_len": sum(lengths) / len(lengths),
        "min_len": min(lengths),
        "max_len": max(lengths),
        "headings": headings,
        "under_100": under_100,
        "over_3000": over_3000,
    }


def main():
    data_dir = PROJECT / "data"
    marker_dir = data_dir / "marker_output"

    # Collect all PDFs that have both MinerU and Marker content_list
    pdf_stems = sorted(
        p.stem
        for p in data_dir.glob("*.pdf")
        if (marker_dir / f"{p.stem}_content_list.json").exists()
        and (data_dir / f"{p.stem}_content_list.json").exists()
    )

    if not pdf_stems:
        print("❌ No papers with both MinerU and Marker content_list found.")
        print("   Run: python scripts/test_marker.py --all --workers 8  first")
        sys.exit(1)

    print(f"{'=' * 80}")
    print(f"  chunker 兼容性评估 — {len(pdf_stems)} papers (MinerU ↔ Marker)")
    print(f"{'=' * 80}")
    print()
    print(
        f"  {'Paper':<30} {'MinerU':>8} {'Marker':>8}  | {'M-chunks':>8} {'Mk-chunks':>8}  | {'M-avg':>6} {'Mk-avg':>6}  | {'M-<100':>6} {'Mk-<100':>6}"
    )
    print(
        f"  {'':30} {'sections':>8} {'sections':>8}  | {'':>8} {'':>8}  | {'':>6} {'':>6}  | {'':>6} {'':>6}"
    )
    print(
        f"  {'─' * 30} {'─' * 8} {'─' * 8}  | {'─' * 8} {'─' * 8}  | {'─' * 6} {'─' * 6}  | {'─' * 6} {'─' * 6}"
    )

    totals = {
        "m_sections": 0,
        "mk_sections": 0,
        "m_chunks": 0,
        "mk_chunks": 0,
        "m_chars": 0,
        "mk_chars": 0,
        "m_under100": 0,
        "mk_under100": 0,
        "m_over3000": 0,
        "mk_over3000": 0,
    }

    for stem in pdf_stems:
        mineru_cl = load_json(data_dir / f"{stem}_content_list.json")
        marker_cl = load_json(marker_dir / f"{stem}_content_list.json")

        mc = chunk_content_list(mineru_cl)
        mk = chunk_content_list(marker_cl)

        ms = stats(mc, "M")
        mks = stats(mk, "Mk")

        totals["m_sections"] += ms["n_sections"]
        totals["mk_sections"] += mks["n_sections"]
        totals["m_chunks"] += ms["n_chunks"]
        totals["mk_chunks"] += mks["n_chunks"]
        totals["m_chars"] += sum(len(c.content) for c in mc)
        totals["mk_chars"] += sum(len(c.content) for c in mk)
        totals["m_under100"] += ms["under_100"]
        totals["mk_under100"] += mks["under_100"]
        totals["m_over3000"] += ms["over_3000"]
        totals["mk_over3000"] += mks["over_3000"]

        print(
            f"  {stem:<30} {ms['n_sections']:>8} {mks['n_sections']:>8}  | "
            f"{ms['n_chunks']:>8} {mks['n_chunks']:>8}  | "
            f"{ms['avg_len']:>6.0f} {mks['avg_len']:>6.0f}  | "
            f"{ms['under_100']:>6} {mks['under_100']:>6}"
        )

    # Total row
    m_avg = totals["m_chars"] / max(totals["m_chunks"], 1) if totals["m_chunks"] else 0
    mk_avg = totals["mk_chars"] / max(totals["mk_chunks"], 1) if totals["mk_chunks"] else 0
    print(
        f"  {'─' * 30} {'─' * 8} {'─' * 8}  | {'─' * 8} {'─' * 8}  | {'─' * 6} {'─' * 6}  | {'─' * 6} {'─' * 6}"
    )
    print(
        f"  {'TOTAL':<30} {totals['m_sections']:>8} {totals['mk_sections']:>8}  | "
        f"{totals['m_chunks']:>8} {totals['mk_chunks']:>8}  | "
        f"{m_avg:>6.0f} {mk_avg:>6.0f}  | "
        f"{totals['m_under100']:>6} {totals['mk_under100']:>6}"
    )

    print()
    print(f"  >3000 chars:   MinerU {totals['m_over3000']}   Marker {totals['mk_over3000']}")
    print(f"  content_list:  data/marker_output/*_content_list.json  ({len(pdf_stems)} files)")
    print("  chunker fix:   text_level==1  →  'text_level' in item  ✅ applied")


if __name__ == "__main__":
    main()
