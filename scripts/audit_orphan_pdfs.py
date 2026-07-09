#!/usr/bin/env python3
"""Audit: find orphan paper.pdf files whose arxiv_id is NOT in Milvus.

Streams ``find`` output through a pipeline — no in-memory list, no
intermediate cache file.  GPFS-native ``find`` is 10-50x faster than
Python ``rglob`` for deep directory trees.

Usage:
  python scripts/audit_orphan_pdfs.py            # full audit
  python scripts/audit_orphan_pdfs.py --dry-run  # first 100 orphans
"""

from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

import structlog

from scholight.logging import configure_logging
from scholight.storage import storage
from scholight.store.client import _resolve_token, _resolve_uri

OUTPUT_PATH = Path("/inspire/hdd/project/multi-agent/niexiaohang-25130061/scholight/data")
BATCH_SIZE = 1000  # Milvus id-in[...] batch

logger = structlog.get_logger(__name__)


def _check_batch(client: object, batch: list[str]) -> set[str]:
    """Return subset of *batch* that EXISTS in Milvus."""
    ids_quoted = ", ".join(f"'{aid}'" for aid in batch)
    rows = client.query(
        "arxiv_papers",
        filter=f"arxiv_id in [{ids_quoted}]",
        output_fields=["arxiv_id"],
        limit=len(batch) + 100,
    )
    return {r["arxiv_id"] for r in rows}


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit orphan PDFs on disk vs Milvus.")
    parser.add_argument("--dry-run", action="store_true", help="Preview only first 100 orphans")
    args = parser.parse_args()

    configure_logging(log_level="INFO", use_json=True)

    papers_root = str(storage._papers_root)
    logger.info("starting find pipeline", root=papers_root)

    # ── Launch find (stdout → Python pipe) ──
    find_proc = subprocess.Popen(
        ["find", papers_root, "-maxdepth", "4", "-name", "paper.pdf", "-type", "f"],
        stdout=subprocess.PIPE,
        text=True,
        bufsize=65536,
    )

    # ── Milvus client ──
    import pymilvus

    client = pymilvus.MilvusClient(uri=_resolve_uri(), token=_resolve_token(), timeout=30)

    # ── Stream pipeline ──
    total_disk = 0
    orphan_count = 0
    t0 = time.monotonic()
    batch: list[tuple[str, str]] = []  # (arxiv_id, path)

    out_file = OUTPUT_PATH / "orphan_pdfs.txt"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w") as fout:
        fout.write("arxiv_id\tpath\tsize_bytes\n")

        for line in find_proc.stdout:
            path_str = line.rstrip("\n")
            # Path: .../papers/YYYY/MM/DD/{safe_id}/paper.pdf
            safe_id = path_str.rsplit("/", 2)[-2]
            arxiv_id = safe_id.replace("_", "/")
            batch.append((arxiv_id, path_str))
            total_disk += 1

            if len(batch) >= BATCH_SIZE:
                batch_ids = [b[0] for b in batch]
                in_db = _check_batch(client, batch_ids)

                for aid, pth in batch:
                    if aid not in in_db:
                        orphan_count += 1
                        try:
                            size = Path(pth).stat().st_size
                        except OSError:
                            size = -1
                        fout.write(f"{aid}\t{pth}\t{size}\n")
                        if args.dry_run and orphan_count >= 100:
                            break

                elapsed = time.monotonic() - t0
                logger.info(
                    "progress",
                    scanned=total_disk,
                    orphans=orphan_count,
                    elapsed_m=f"{elapsed / 60:.1f}",
                )

                batch = []
                if args.dry_run and orphan_count >= 100:
                    find_proc.kill()
                    break

        # Final flush
        if batch:
            batch_ids = [b[0] for b in batch]
            in_db = _check_batch(client, batch_ids)
            for aid, pth in batch:
                if aid not in in_db:
                    orphan_count += 1
                    try:
                        size = Path(pth).stat().st_size
                    except OSError:
                        size = -1
                    fout.write(f"{aid}\t{pth}\t{size}\n")

    find_proc.wait(timeout=10)

    elapsed = time.monotonic() - t0
    logger.info(
        "audit complete",
        total_disk=total_disk,
        orphans=orphan_count,
        elapsed_m=f"{elapsed / 60:.1f}",
    )

    print(f"\nTotal PDFs on disk:  {total_disk}")
    print(f"Orphans (no DB row): {orphan_count}")
    print(f"Elapsed:             {elapsed / 60:.1f} min")
    print(f"Output:              {out_file}")


if __name__ == "__main__":
    main()
