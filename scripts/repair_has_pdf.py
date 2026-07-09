#!/usr/bin/env python3
"""Audit + repair: verify on-disk PDFs for Milvus papers with has_pdf=False.

Approach (efficient):
  1. Cursor-scan Milvus for has_pdf==False → get arxiv_id + created.
  2. For each paper: compute expected PDF path → check if exists + valid.
  3. Output TXT report: orphans (DB row, no PDF) + broken PDFs + ready-to-fix.
  4. Optionally apply --fix to partial_update has_pdf=True for valid PDFs.

This avoids scanning 2.5M+ files on disk — we only touch the paths that
Milvus says should have a PDF.

Usage:
  python scripts/repair_has_pdf.py                                   # audit only
  python scripts/repair_has_pdf.py --fix                             # audit + fix
  python scripts/repair_has_pdf.py --dry-run                         # first 1000
"""

from __future__ import annotations

import argparse
import time
from multiprocessing.pool import ThreadPool
from pathlib import Path

import structlog

from compass.logging import configure_logging
from compass.storage import storage
from compass.store.client import _WRITE_LOCK, _resolve_token, _resolve_uri

OUTPUT_DIR = Path("/inspire/hdd/project/multi-agent/niexiaohang-25130061/academic-compass/data")
GATHER_PAGE = 10000
FIX_WORKERS = 4  # Thread count for I/O check (disk access)
MILVUS_BATCH = 100  # partial_update batch size
BROKEN_THRESHOLD = 512  # bytes — anything smaller is almost certainly corrupt

logger = structlog.get_logger(__name__)


# ── Disk check ────────────────────────────────────────────────────────────────


def _pdf_ok(arxiv_id: str, created: str) -> tuple[str, str]:
    """Check if the PDF on disk is valid.

    Returns one of: "ok", "missing", "broken", "no_created".
    """
    if not created:
        return "no_created", ""
    dest = storage.pdf_path(arxiv_id, created)
    if not dest.exists():
        return "missing", str(dest)
    size = dest.stat().st_size
    if size < BROKEN_THRESHOLD:
        return "broken", f"{dest} ({size}B)"
    # Quick %PDF- magic-number check (first 4 bytes)
    try:
        with open(dest, "rb") as fh:
            header = fh.read(5)
        if not header.startswith(b"%PDF"):
            return "broken", f"{dest} (bad header: {header[:20]!r})"
    except OSError as exc:
        return "broken", str(exc)
    return "ok", str(dest)


# ── Milvus fix ────────────────────────────────────────────────────────────────


def _batch_fix(arxiv_ids: list[str]) -> int:
    """partial_update has_pdf=True for a batch. Returns updated count."""
    import pymilvus

    client = pymilvus.MilvusClient(uri=_resolve_uri(), token=_resolve_token(), timeout=30)
    total = 0
    for i in range(0, len(arxiv_ids), MILVUS_BATCH):
        batch = arxiv_ids[i : i + MILVUS_BATCH]
        try:
            with _WRITE_LOCK:
                result = client.upsert(
                    "arxiv_papers",
                    data=[{"arxiv_id": aid, "has_pdf": True} for aid in batch],
                    partial_update=True,
                )
                total += result.get("upsert_count", len(batch))
        except Exception as exc:
            logger.error(
                "fix batch failed", first_id=batch[0], batch_size=len(batch), error=str(exc)[:200]
            )
    return total


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit + repair has_pdf for arXiv papers.")
    parser.add_argument("--fix", action="store_true", help="Apply has_pdf=True for valid PDFs")
    parser.add_argument("--dry-run", action="store_true", help="Limit to first 1000 papers")
    args = parser.parse_args()

    log_path = storage.log_path("repair_pdf", "repair_has_pdf.log")
    configure_logging(log_level="INFO", use_json=True, file_handler=(str(log_path), 50_000_000, 5))

    # ── 1. Gather from Milvus ──
    import pymilvus

    client = pymilvus.MilvusClient(uri=_resolve_uri(), token=_resolve_token(), timeout=30)
    papers: list[dict[str, str]] = []
    last_id = "!"
    t0 = time.monotonic()

    logger.info("gathering has_pdf=False papers from Milvus")
    while True:
        rows = client.query(
            "arxiv_papers",
            filter=f"has_pdf == false and arxiv_id > '{last_id}'",
            output_fields=["arxiv_id", "created"],
            limit=GATHER_PAGE,
        )
        if not rows:
            break
        for r in rows:
            papers.append({"arxiv_id": r["arxiv_id"], "created": r.get("created", "")})
            last_id = r["arxiv_id"]
        if args.dry_run and len(papers) >= 1000:
            break
        elapsed = time.monotonic() - t0
        logger.info("gather progress", scanned=len(papers), elapsed_s=f"{elapsed:.0f}")

    elapsed = time.monotonic() - t0
    logger.info("gather complete", total=len(papers), elapsed_s=f"{elapsed:.0f}")

    # ── 2. Check disk in parallel ──
    logger.info("checking PDFs on disk", total=len(papers))
    t0 = time.monotonic()
    ok_ids: list[str] = []
    missing: list[tuple[str, str]] = []
    broken: list[tuple[str, str, str]] = []
    no_created: list[str] = []

    def _worker(p: dict[str, str]) -> tuple[str, str, str]:
        aid = p["arxiv_id"]
        status, detail = _pdf_ok(aid, p["created"])
        return aid, status, detail

    with ThreadPool(FIX_WORKERS) as pool:
        for i, (aid, status, detail) in enumerate(
            pool.imap_unordered(_worker, papers, chunksize=500)
        ):
            if status == "ok":
                ok_ids.append(aid)
            elif status == "missing":
                missing.append((aid, detail))
            elif status == "broken":
                broken.append((aid, detail, aid))
            elif status == "no_created":
                no_created.append(aid)

            if (i + 1) % 50000 == 0:
                elapsed = time.monotonic() - t0
                logger.info(
                    "check progress",
                    checked=i + 1,
                    ok=len(ok_ids),
                    missing=len(missing),
                    broken=len(broken),
                    elapsed_m=f"{elapsed / 60:.1f}",
                )

    elapsed = time.monotonic() - t0
    logger.info(
        "disk check complete",
        ok=len(ok_ids),
        missing=len(missing),
        broken=len(broken),
        no_created=len(no_created),
        elapsed_m=f"{elapsed / 60:.1f}",
    )

    # ── 3. Write report ──
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # orphans.txt: missing on disk
    with open(OUTPUT_DIR / "orphan_pdfs.txt", "w") as f:
        f.write("arxiv_id\tpath\n")
        for aid, path in missing:
            f.write(f"{aid}\t{path}\n")

    # broken_pdfs.txt: on disk but corrupt
    broken = [
        (aid, detail.split("(")[0].strip() if "(" in detail else detail, detail)
        for aid, detail, _ in broken
    ]
    with open(OUTPUT_DIR / "broken_pdfs.txt", "w") as f:
        f.write("arxiv_id\tpath\tdetail\n")
        for aid, path, detail in broken:
            f.write(f"{aid}\t{path}\t{detail}\n")

    # no_created.txt: has_pdf=False but no created date → can't compute path
    with open(OUTPUT_DIR / "no_created_pdfs.txt", "w") as f:
        f.write("arxiv_id\n")
        for aid in no_created:
            f.write(f"{aid}\n")

    # ── 4. Fix (if requested) ──
    if args.fix and ok_ids:
        logger.info("applying fixes", to_fix=len(ok_ids))
        updated = _batch_fix(ok_ids)
        logger.info("fixes applied", updated=updated)
    elif ok_ids:
        # Write ready-to-fix list
        with open(OUTPUT_DIR / "ready_to_fix.txt", "w") as f:
            f.write("arxiv_id\n")
            for aid in ok_ids:
                f.write(f"{aid}\n")
        logger.info("ready to fix", count=len(ok_ids), file="data/ready_to_fix.txt")

    # ── Summary ──
    print(f"\n{'=' * 50}")
    print(f"  Total has_pdf=False:   {len(papers)}")
    print(f"  PDF OK on disk:        {len(ok_ids)}  ← {'FIXED' if args.fix else 'ready to fix'}")
    print(f"  PDF MISSING:           {len(missing)}  → data/orphan_pdfs.txt")
    print(f"  PDF BROKEN:            {len(broken)}   → data/broken_pdfs.txt")
    print(f"  No created date:       {len(no_created)} → data/no_created_pdfs.txt")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
