#!/usr/bin/env python3
"""Clean up non-canonical paper directories on disk.

Strategy:
  For each non-canonical directory (safe_id not matching canonicalize_arxiv_id):
    - If canonical dir already exists with valid PDF → DELETE the old dir
    - If canonical dir does NOT exist → RENAME → canonical

  Also: partial_update has_pdf=True for any canonical ID that's still False.

Usage:
  python scripts/diagnose_bad_dirs.py              # audit only
  python scripts/diagnose_bad_dirs.py --apply       # execute cleanup
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import structlog

from compass.logging import configure_logging
from compass.sources.arxiv import canonicalize_arxiv_id
from compass.storage import storage
from compass.store.client import _WRITE_LOCK, _resolve_token, _resolve_uri

OUTPUT_DIR = Path("/inspire/hdd/project/multi-agent/niexiaohang-25130061/academic-compass/data")

logger = structlog.get_logger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _safe_to_canon(safe_id: str) -> str | None:
    with_slash = safe_id.replace("_", "/", 1)
    return canonicalize_arxiv_id(with_slash)


def _valid_pdf(p: Path) -> bool:
    try:
        return p.stat().st_size >= 512 and open(p, "rb").read(5).startswith(b"%PDF")
    except OSError:
        return False


# ── Scan ──────────────────────────────────────────────────────────────────────


def _scan() -> list[tuple[str, Path]]:
    """Find (canonical_id, old_dir) for every non-canonical paper directory."""
    import subprocess as sp

    root = str(storage._papers_root)
    proc = sp.Popen(
        ["find", root, "-maxdepth", "5", "-name", "paper.pdf", "-type", "f"],
        stdout=sp.PIPE,
        text=True,
        bufsize=65536,
    )

    results: list[tuple[str, Path]] = []
    total = 0
    t0 = time.monotonic()

    for line in proc.stdout:
        total += 1
        parent = Path(line.rstrip("\n")).parent
        safe_id = parent.name
        canon = _safe_to_canon(safe_id)
        if canon is None:
            continue
        if canon.replace("/", "_") == safe_id:
            continue
        results.append((canon, parent))

        if total % 200000 == 0:
            logger.info(
                "scan", files=total, non_canon=len(results), s=f"{time.monotonic() - t0:.0f}"
            )

    proc.wait(timeout=10)
    logger.info("scan complete", files=total, non_canon=len(results))
    return results


# ── Milvus ────────────────────────────────────────────────────────────────────


def _batch_fix(arxiv_ids: list[str]) -> int:
    import pymilvus

    client = pymilvus.MilvusClient(uri=_resolve_uri(), token=_resolve_token(), timeout=30)
    total = 0
    for i in range(0, len(arxiv_ids), 100):
        batch = arxiv_ids[i : i + 100]
        try:
            with _WRITE_LOCK:
                r = client.upsert(
                    "arxiv_papers",
                    data=[{"arxiv_id": a, "has_pdf": True} for a in batch],
                    partial_update=True,
                )
                total += r.get("upsert_count", len(batch))
        except Exception as exc:
            logger.error("fix batch", first=batch[0], error=str(exc)[:200])
    return total


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    configure_logging(log_level="INFO", use_json=True)

    # 1. Scan
    non_canon = _scan()
    if not non_canon:
        print("All directories canonical — nothing to do.")
        return

    # 2. Plan
    to_delete: list[tuple[Path, str]] = []  # (old_dir, arxiv_id)
    to_rename: list[tuple[Path, Path, str]] = []  # (old, new, arxiv_id)
    fix_ids: list[str] = []

    import pymilvus

    client = pymilvus.MilvusClient(uri=_resolve_uri(), token=_resolve_token(), timeout=30)

    # Batch Milvus check for all canonical IDs
    all_canon = list({c for c, _ in non_canon})
    needs_pdf: set[str] = set()
    for i in range(0, len(all_canon), 2000):
        batch = all_canon[i : i + 2000]
        ids_q = ", ".join(f"'{a}'" for a in batch)
        rows = client.query(
            "arxiv_papers",
            filter=f"arxiv_id in [{ids_q}]",
            output_fields=["arxiv_id", "has_pdf"],
            limit=len(batch) + 100,
        )
        for r in rows:
            if not r.get("has_pdf", True):
                needs_pdf.add(r["arxiv_id"])

    for canon_id, old_dir in non_canon:
        canon_safe = canon_id.replace("/", "_")
        canon_dir = old_dir.parent / canon_safe

        if canon_dir.exists() and _valid_pdf(canon_dir / "paper.pdf"):
            to_delete.append((old_dir, canon_id))
        else:
            to_rename.append((old_dir, canon_dir, canon_id))

        if canon_id in needs_pdf:
            fix_ids.append(canon_id)

    # Dedup fix_ids
    fix_ids = sorted(set(fix_ids))

    logger.info("plan", delete=len(to_delete), rename=len(to_rename), fix=len(fix_ids))

    # 3. Output
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_DIR / "cleanup_delete.txt", "w") as f:
        f.write("path\tarxiv_id\n")
        for d, aid in to_delete:
            f.write(f"{d}\t{aid}\n")
    with open(OUTPUT_DIR / "cleanup_rename.txt", "w") as f:
        f.write("old_path\tnew_path\tarxiv_id\n")
        for o, n, aid in to_rename:
            f.write(f"{o}\t{n}\t{aid}\n")

    # 4. Apply
    if args.apply:
        deleted = 0
        for d, _ in to_delete:
            try:
                import shutil

                shutil.rmtree(d)
                deleted += 1
            except OSError as exc:
                logger.warning("delete failed", path=str(d), error=str(exc))
        logger.info("deletes done", deleted=deleted)

        renamed = 0
        for o, n, _ in to_rename:
            try:
                os.rename(str(o), str(n))
                renamed += 1
            except OSError as exc:
                logger.warning("rename failed", old=str(o), new=str(n), error=str(exc))
        logger.info("renames done", renamed=renamed)

        if fix_ids:
            updated = _batch_fix(fix_ids)
            logger.info("milvus fixes", updated=updated, fixable=len(fix_ids))

    print(f"\n{'=' * 55}")
    print(f"  Non-canonical dirs:   {len(non_canon)}")
    print(f"  → DELETE (dup):      {len(to_delete)}")
    print(f"  → RENAME (orphan):   {len(to_rename)}")
    print(f"  → Milvus fix:        {len(fix_ids)}")
    if not args.apply:
        print("\n  Re-run with --apply to execute.")
    print(f"{'=' * 55}")


if __name__ == "__main__":
    main()
