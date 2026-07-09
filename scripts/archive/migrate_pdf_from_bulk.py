#!/usr/bin/env python3
"""Migrate PDFs from arxiv_bulk tar archives to paper storage directories.

Two-phase approach:
  Phase 1 — Build Tar Index: scan all .tar files, build SQLite index
           mapping arxiv_id → (tar_path, member_name).  One-time, reusable.
  Phase 2 — Gather & Migrate: cursor-iterate Milvus papers with has_pdf=False,
           match against tar index, extract PDFs from tars, copy to paper
           directories, update has_pdf=True in Milvus.

Usage:
  python scripts/migrate_pdf_from_bulk.py build-index    # Build tar index
  python scripts/migrate_pdf_from_bulk.py migrate         # Migrate PDFs
  python scripts/migrate_pdf_from_bulk.py migrate --dry-run  # Preview only
  python scripts/migrate_pdf_from_bulk.py status          # Show stats

Design principles:
  - arxiv_bulk is READ-ONLY — never modified.
  - Milvus is the source of truth — papers not in DB are skipped.
  - Each tar file is opened exactly once during migration.
  - All Milvus updates use partial_update=True — safe, no vector read needed.
  - Detailed structured logging for long-running observation.
  - Checkpoint file for resume after interruption.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tarfile
import time
import traceback
from pathlib import Path
from typing import Any

import structlog

from compass.logging import configure_logging
from compass.storage import storage
from compass.store.client import _WRITE_LOCK, _resolve_token, _resolve_uri, get_client

# ── Module-level logger ──────────────────────────────────────────────────────

logger = structlog.get_logger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

ARXIV_BULK_DIR = Path("/inspire/hdd/global_public/niexiaohang/arxiv_bulk")
INDEX_DB_PATH = Path(__file__).resolve().parent / "tar_index.db"
CHECKPOINT_PATH = Path(__file__).resolve().parent / ".migrate_pdf_checkpoint.json"

MILVUS_UPDATE_BATCH: int = 100  # Papers per Milvus upsert batch
TAR_PROGRESS_INTERVAL: int = 10  # Log every N tars during migration

# Regex: legacy arXiv member name "category_prefix" + "digits" + ".pdf"
#  e.g. "astro-ph9704156.pdf" → category="astro-ph", digits="9704156"
# Note: applied AFTER stripping ".pdf" suffix, so pattern does NOT include \.pdf$
_LEGACY_SPLIT_RE = re.compile(r"^([a-z][a-z-]*?)(\d{4,})$")


# ── arXiv ID Parsing ─────────────────────────────────────────────────────────


def parse_arxiv_id_from_member(member_name: str) -> str | None:
    """Parse an arXiv ID from a tar member path.

    Handles both formats found in arxiv_bulk tars:

    Modern (post-2007):
        '2403/2403.00001.pdf'          → '2403.00001'
        '1901/1901.00293.pdf'          → '1901.00293'

    Legacy (pre-2007):
        '9704/astro-ph9704156.pdf'     → 'astro-ph/9704156'
        '9704/dg-ga9704007.pdf'        → 'dg-ga/9704007'
        '9704/physics9704008.pdf'      → 'physics/9704008'

    Returns None for non-PDF files or unparseable names.
    """
    # Isolate filename (strip directory prefix like '9704/')
    filename = member_name.rsplit("/", 1)[-1]
    if not filename.lower().endswith(".pdf"):
        return None

    name = filename[:-4]  # strip '.pdf'

    # Modern format: contains a dot → '2403.00001'
    if "." in name:
        return name

    # Legacy format: category prefix + year-month-sequence digits
    m = _LEGACY_SPLIT_RE.match(name)
    if m:
        return f"{m.group(1)}/{m.group(2)}"

    logger.warning("unparseable member name", member_name=member_name)
    return None


# ── SQLite Tar Index ─────────────────────────────────────────────────────────


def _open_index_db(db_path: Path) -> sqlite3.Connection:
    """Open the tar index database with WAL mode for performance."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-20000")  # ~20MB cache
    return conn


def build_tar_index(bulk_dir: Path, index_db: Path, max_tars: int = 0) -> int:
    """Phase 1: scan all .tar files, build SQLite index.

    One-time operation.  Safe to re-run — uses INSERT OR IGNORE.

    If *max_tars* > 0, only process the first *max_tars* files (for testing).

    Returns total number of arXiv IDs indexed.
    """
    tar_files = sorted(bulk_dir.glob("arXiv_pdf_*.tar"))
    if max_tars > 0:
        tar_files = tar_files[:max_tars]
    if not tar_files:
        logger.error("no tar files found", bulk_dir=str(bulk_dir))
        sys.exit(1)

    logger.info("building tar index", tar_count=len(tar_files), bulk_dir=str(bulk_dir))

    conn = _open_index_db(index_db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tar_index (
            arxiv_id    TEXT PRIMARY KEY,
            tar_path    TEXT NOT NULL,
            member_name TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tar_path ON tar_index(tar_path)")
    conn.commit()

    total_ids = 0
    failed_tars = 0
    t0 = time.monotonic()

    for i, tar_file in enumerate(tar_files):
        t_tar_start = time.monotonic()

        try:
            with tarfile.open(tar_file, "r:") as tf:
                members = tf.getmembers()
        except (tarfile.TarError, OSError) as exc:
            logger.warning(
                "failed to open tar, skipping",
                tar=str(tar_file.name),
                error=str(exc)[:120],
            )
            failed_tars += 1
            continue

        rows: list[tuple[str, str, str]] = []
        for member in members:
            if not member.isfile():
                continue
            arxiv_id = parse_arxiv_id_from_member(member.name)
            if arxiv_id is None:
                continue
            rows.append((arxiv_id, str(tar_file), member.name))

        if rows:
            conn.executemany(
                "INSERT OR IGNORE INTO tar_index(arxiv_id, tar_path, member_name) VALUES (?, ?, ?)",
                rows,
            )
            conn.commit()
            total_ids += len(rows)

        # Progress logging every 100 tars or at end
        if (i + 1) % 100 == 0 or (i + 1) == len(tar_files):
            elapsed = time.monotonic() - t0
            rate = total_ids / elapsed if elapsed > 0 else 0
            logger.info(
                "tar index progress",
                tars_processed=f"{i + 1}/{len(tar_files)}",
                total_ids=total_ids,
                failed_tars=failed_tars,
                elapsed_m=f"{elapsed / 60:.1f}",
                ids_per_sec=f"{rate:.0f}",
                last_tar=str(tar_file.name),
                last_tar_ids=len(rows),
                last_tar_ms=f"{(time.monotonic() - t_tar_start) * 1000:.0f}",
            )

    conn.close()
    elapsed = time.monotonic() - t0
    logger.info(
        "tar index complete",
        total_ids=total_ids,
        failed_tars=failed_tars,
        elapsed_m=f"{elapsed / 60:.1f}",
        ids_per_sec=f"{total_ids / elapsed:.0f}" if elapsed > 0 else "N/A",
    )
    return total_ids


# ── Migration: Gather Work List ──────────────────────────────────────────────

# Milvus query() cursor pagination with server-side BOOL filter.
# Cursor (arxiv_id > 'X') avoids offset overhead; has_pdf==false filter
# avoids scanning papers that already have PDFs.  Page size fits well
# within Milvus's query window (offset+limit ≤ 16384).
GATHER_PAGE_SIZE: int = 10000


def _query_papers_page(
    client: Any,
    last_id: str,
) -> list[dict[str, Any]]:
    """Fetch one page: has_pdf==false AND arxiv_id > last_id.

    Both filters are server-side — no full-scan, no client-side discard.
    """
    try:
        result: list[dict[str, Any]] = client.query(
            "arxiv_papers",
            filter=f"has_pdf == false and arxiv_id > '{last_id}'",
            output_fields=["arxiv_id", "created"],
            limit=GATHER_PAGE_SIZE,
        )
        return result
    except Exception as exc:
        logger.error(
            "milvus query failed during gather",
            last_id=last_id,
            error=str(exc)[:200],
        )
        raise


def gather_work_list(index_db: Path, max_papers: int = 0) -> dict[str, list[tuple[str, str, str]]]:
    """Pass 2a: Cursor-iterate papers with has_pdf=False, match against tar index.

    Uses server-side ``has_pdf == false and arxiv_id > 'X'`` — 10k per page,
    no offset overhead, no client-side filtering, no full-scan hangs.

    If *max_papers* > 0, stop gathering after *max_papers* matched (for testing).
    """
    if not index_db.exists():
        logger.error("tar index not found", path=str(index_db))
        logger.error("run 'build-index' first")
        sys.exit(1)

    conn = sqlite3.connect(str(index_db))
    conn.row_factory = sqlite3.Row

    # Use dedicated MilvusClient (not singleton) — long-running gather
    # needs a fresh gRPC channel to avoid stale connections from prior runs.
    import pymilvus

    client = pymilvus.MilvusClient(uri=_resolve_uri(), token=_resolve_token(), timeout=30)

    work: dict[str, list[tuple[str, str, str]]] = {}
    last_id = "!"
    total_matched = 0
    total_scanned = 0
    t0 = time.monotonic()

    logger.info(
        "gathering work list — scanning Milvus (has_pdf=false) + matching against tar index"
    )

    while True:
        papers = _query_papers_page(client, last_id)
        if not papers:
            break

        for paper in papers:
            total_scanned += 1
            arxiv_id: str = paper["arxiv_id"]
            created: str = paper.get("created", "")
            last_id = arxiv_id

            # SQLite PK lookup (B-tree, ~147万 rows, fast per-row)
            try:
                row = conn.execute(
                    "SELECT tar_path, member_name FROM tar_index WHERE arxiv_id = ?",
                    (arxiv_id,),
                ).fetchone()
            except sqlite3.Error as exc:
                logger.warning("sqlite lookup failed", arxiv_id=arxiv_id, error=str(exc)[:120])
                continue

            if row is None:
                continue

            tar_path: str = row["tar_path"]
            member_name: str = row["member_name"]
            work.setdefault(tar_path, []).append((arxiv_id, created, member_name))
            total_matched += 1

            if max_papers > 0 and total_matched >= max_papers:
                break

        if max_papers > 0 and total_matched >= max_papers:
            break

        # Progress log every page
        elapsed = time.monotonic() - t0
        logger.info(
            "gather progress",
            scanned=total_scanned,
            matched=total_matched,
            matched_pct=f"{total_matched / max(total_scanned, 1) * 100:.1f}",
            tars=len(work),
            elapsed_m=f"{elapsed / 60:.1f}",
            last_id=last_id,
        )

    conn.close()
    elapsed = time.monotonic() - t0
    logger.info(
        "gather complete",
        total_papers_scanned=total_scanned,
        total_matched=total_matched,
        matched_pct=f"{total_matched / max(total_scanned, 1) * 100:.1f}",
        tars_to_process=len(work),
        elapsed_m=f"{elapsed / 60:.1f}",
    )
    return work


# ── Migration: Update Milvus Batch ───────────────────────────────────────────


def batch_set_has_pdf(arxiv_ids: list[str]) -> int:
    """Batch update has_pdf=True for multiple papers using partial update.

    Uses Milvus 3.0 partial_update=True — only sends arxiv_id + has_pdf.
    Does NOT read full row, does NOT touch vector embeddings or any other field.
    Updates in batches of MILVUS_UPDATE_BATCH.

    Returns total number of papers successfully updated.
    """
    if not arxiv_ids:
        return 0

    # Dedicated client — avoid singleton's potentially stale gRPC channel
    # from prior process crashes (same pattern as gather_work_list).
    import pymilvus

    client = pymilvus.MilvusClient(uri=_resolve_uri(), token=_resolve_token(), timeout=30)
    total_updated = 0

    for i in range(0, len(arxiv_ids), MILVUS_UPDATE_BATCH):
        batch_ids = arxiv_ids[i : i + MILVUS_UPDATE_BATCH]
        batch_data = [{"arxiv_id": aid, "has_pdf": True} for aid in batch_ids]

        try:
            with _WRITE_LOCK:
                result = client.upsert(
                    "arxiv_papers",
                    data=batch_data,
                    partial_update=True,
                )
                upserted = result.get("upsert_count", len(batch_ids))
                total_updated += upserted
        except Exception as exc:
            logger.error(
                "milvus partial update failed",
                batch_size=len(batch_ids),
                first_id=batch_ids[0],
                error=str(exc)[:200],
            )

    return total_updated


# ── Migration: Process Tars ─────────────────────────────────────────────────


def _load_checkpoint() -> set[str]:
    """Load set of already-processed tar paths from checkpoint file."""
    if not CHECKPOINT_PATH.exists():
        return set()
    try:
        data = json.loads(CHECKPOINT_PATH.read_text())
        return set(data.get("completed_tars", []))
    except (json.JSONDecodeError, KeyError) as exc:
        logger.warning("corrupt checkpoint, starting fresh", error=str(exc)[:120])
        return set()


def _save_checkpoint(completed: set[str]) -> None:
    """Atomically save the set of completed tar paths."""
    tmp = CHECKPOINT_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {"completed_tars": sorted(completed)},
            indent=2,
        )
    )
    os.replace(tmp, CHECKPOINT_PATH)


def migrate_pdfs(
    work: dict[str, list[tuple[str, str, str]]],
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Pass 2b: Process each tar, extract PDFs, copy to paper dirs, update Milvus.

    Args:
        work: tar_path → [(arxiv_id, created, member_name), ...]
        dry_run: If True, only log what WOULD be done without actual changes.

    Returns:
        Stats dict with keys: tars_processed, pdfs_copied, pdfs_failed,
        milvus_updated, tars_failed.
    """
    completed_tars = _load_checkpoint()
    tar_paths = sorted(work.keys())

    stats = {
        "tars_processed": 0,
        "tars_skipped": 0,
        "pdfs_copied": 0,
        "pdfs_failed": 0,
        "milvus_updated": 0,
        "tars_failed": 0,
    }

    t0 = time.monotonic()

    logger.info(
        "starting migration",
        total_tars=len(tar_paths),
        already_completed=len(completed_tars),
        papers_to_migrate=sum(len(v) for v in work.values()),
        dry_run=dry_run,
    )

    for idx, tar_path_str in enumerate(tar_paths):
        # Skip already-completed tars
        if tar_path_str in completed_tars:
            stats["tars_skipped"] += 1
            continue

        tar_path = Path(tar_path_str)
        papers_in_tar = work[tar_path_str]
        tar_pdf_count = len(papers_in_tar)

        if not tar_path.exists():
            logger.error(
                "tar file not found on disk, skipping",
                tar=str(tar_path),
                papers_in_tar=tar_pdf_count,
            )
            stats["tars_failed"] += 1
            continue

        logger.info(
            "processing tar",
            tar=str(tar_path.name),
            papers=tar_pdf_count,
            progress=f"{idx + 1}/{len(tar_paths)}",
        )

        # Build lookup: member_name → (arxiv_id, created)
        # We need this because tar extraction iterates by member, and we
        # need to map member_name → arxiv_id → storage path.
        member_map: dict[str, tuple[str, str]] = {}
        for arxiv_id, created, member_name in papers_in_tar:
            member_map[member_name] = (arxiv_id, created)

        # Open tar once, extract all needed members
        extracted_ids: list[str] = []  # arxiv_ids whose PDF was successfully copied
        t_tar_start = time.monotonic()

        try:
            with tarfile.open(tar_path, "r:") as tf:
                for member in tf.getmembers():
                    if member.name not in member_map:
                        continue

                    arxiv_id, created = member_map[member.name]

                    # Validate created date
                    if not created:
                        logger.warning(
                            "paper has no created date, skipping",
                            arxiv_id=arxiv_id,
                        )
                        continue

                    if dry_run:
                        dest = storage.pdf_path(arxiv_id, created)
                        logger.info(
                            "DRY RUN: would copy",
                            arxiv_id=arxiv_id,
                            member=member.name,
                            dest=str(dest),
                        )
                        extracted_ids.append(arxiv_id)
                        continue

                    # Verify member is a regular file
                    if not member.isfile():
                        logger.warning(
                            "skipping non-file member",
                            arxiv_id=arxiv_id,
                            member=member.name,
                        )
                        continue

                    # Read PDF bytes directly from tar stream (no temp files)
                    try:
                        reader = tf.extractfile(member)
                        if reader is None:
                            logger.warning(
                                "extractfile returned None",
                                arxiv_id=arxiv_id,
                                member=member.name,
                            )
                            stats["pdfs_failed"] += 1
                            continue
                        pdf_bytes = reader.read()
                    except (tarfile.TarError, OSError) as exc:
                        logger.error(
                            "failed to read from tar",
                            arxiv_id=arxiv_id,
                            member=member.name,
                            error=str(exc)[:120],
                        )
                        stats["pdfs_failed"] += 1
                        continue

                    if not pdf_bytes:
                        logger.warning(
                            "extracted PDF is empty",
                            arxiv_id=arxiv_id,
                        )
                        stats["pdfs_failed"] += 1
                        continue

                    # Write to paper directory
                    dest = storage.pdf_path(arxiv_id, created)
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(pdf_bytes)

                        extracted_ids.append(arxiv_id)
                        stats["pdfs_copied"] += 1

                    except OSError as exc:
                        logger.error(
                            "failed to write PDF",
                            arxiv_id=arxiv_id,
                            dest=str(dest),
                            error=str(exc)[:120],
                        )
                        stats["pdfs_failed"] += 1
                        continue

            # After tar is processed, update Milvus for this tar's papers
            if extracted_ids and not dry_run:
                updated = batch_set_has_pdf(extracted_ids)
                stats["milvus_updated"] += updated
                if updated < len(extracted_ids):
                    logger.warning(
                        "partial milvus update",
                        extracted=len(extracted_ids),
                        updated=updated,
                        tar=str(tar_path.name),
                    )

            # Mark tar as completed
            completed_tars.add(tar_path_str)
            _save_checkpoint(completed_tars)
            stats["tars_processed"] += 1

            # Progress log
            elapsed_tar = time.monotonic() - t_tar_start
            if (idx + 1) % TAR_PROGRESS_INTERVAL == 0 or (idx + 1) == len(tar_paths):
                total_elapsed = time.monotonic() - t0
                # Consider: tars_processed + tars_skipped gives actual progress
                effective_tars = stats["tars_processed"] + stats["tars_skipped"]
                est_total_tars = len(tar_paths) - stats["tars_skipped"]
                if effective_tars > 0 and stats["tars_processed"] > 0:
                    remaining = est_total_tars - stats["tars_processed"]
                    eta = (total_elapsed / stats["tars_processed"]) * remaining
                else:
                    eta = 0

                logger.info(
                    "migration progress",
                    tars_done=f"{effective_tars}/{len(tar_paths)}",
                    pdfs_copied=stats["pdfs_copied"],
                    pdfs_failed=stats["pdfs_failed"],
                    milvus_updated=stats["milvus_updated"],
                    elapsed_m=f"{total_elapsed / 60:.1f}",
                    eta_m=f"{eta / 60:.1f}" if eta else "N/A",
                    last_tar=str(tar_path.name),
                    last_tar_papers=tar_pdf_count,
                    last_tar_seconds=f"{elapsed_tar:.1f}",
                )

        except (tarfile.TarError, OSError) as exc:
            logger.error(
                "failed to process tar",
                tar=str(tar_path.name),
                error=str(exc)[:200],
                traceback=traceback.format_exc()[:500],
            )
            stats["tars_failed"] += 1
            continue

    total_elapsed = time.monotonic() - t0
    logger.info(
        "migration complete",
        **stats,
        elapsed_m=f"{total_elapsed / 60:.1f}",
        pdfs_per_sec=f"{stats['pdfs_copied'] / total_elapsed:.1f}" if total_elapsed > 0 else "N/A",
    )
    return stats


# ── Status ───────────────────────────────────────────────────────────────────


def show_status(index_db: Path) -> None:
    """Display current state: index size, migration progress, DB counts."""
    # Index stats
    if index_db.exists():
        conn = sqlite3.connect(str(index_db))
        conn.row_factory = sqlite3.Row
        total = conn.execute("SELECT COUNT(*) AS cnt FROM tar_index").fetchone()["cnt"]
        unique_tars = conn.execute(
            "SELECT COUNT(DISTINCT tar_path) AS cnt FROM tar_index"
        ).fetchone()["cnt"]
        conn.close()
        logger.info(
            "tar index stats",
            total_arxiv_ids=total,
            unique_tars=unique_tars,
        )
    else:
        logger.warning("tar index not found", path=str(index_db))

    # Checkpoint stats
    completed = _load_checkpoint()
    logger.info("checkpoint stats", completed_tars=len(completed))

    # Milvus stats: cursor-scan has_pdf=false papers
    client = get_client()
    total_papers = 0
    missing_pdf = 0
    last_id = "!"
    stats = client.get_collection_stats("arxiv_papers")
    total_papers = stats.get("row_count", 0)

    while True:
        rows = client.query(
            "arxiv_papers",
            filter=f"has_pdf == false and arxiv_id > '{last_id}'",
            output_fields=["arxiv_id", "has_pdf"],
            limit=10000,
        )
        if not rows:
            break
        missing_pdf += len(rows)
        last_id = rows[-1]["arxiv_id"]
        # Safety limit
        if missing_pdf >= 500_000:
            logger.info("status scan stopped at 500k missing papers")
            break

    logger.info(
        "database stats",
        papers_scanned=total_papers,
        has_pdf_false=missing_pdf,
        has_pdf_false_pct=f"{missing_pdf / max(total_papers, 1) * 100:.1f}",
    )


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate PDFs from arxiv_bulk tars to paper storage directories.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # build-index
    sub.add_parser("build-index", help="Build tar index SQLite database")

    # migrate
    migrate_parser = sub.add_parser("migrate", help="Migrate PDFs")
    migrate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be done without making changes",
    )

    # status
    sub.add_parser("status", help="Show migration status and DB stats")

    args = parser.parse_args()

    # Configure logging
    log_path = storage.log_path("migrate_pdf", "migrate_pdf_from_bulk.log")
    configure_logging(
        log_level="INFO",
        use_json=True,
        file_handler=(str(log_path), 50_000_000, 5),
    )

    if args.command == "build-index":
        logger.info("=== Phase 1: Build Tar Index ===")
        total = build_tar_index(ARXIV_BULK_DIR, INDEX_DB_PATH)
        logger.info("build-index done", total_ids=total)
        print(f"Index built: {total} arXiv IDs indexed in {INDEX_DB_PATH}")

    elif args.command == "migrate":
        logger.info("=== Phase 2a: Gather Work List ===")
        work = gather_work_list(INDEX_DB_PATH)
        total_papers = sum(len(v) for v in work.values())
        if total_papers == 0:
            logger.info("no papers to migrate — all PDFs already present")
            print("No papers to migrate.")
            return

        logger.info("=== Phase 2b: Migrate PDFs ===")
        stats = migrate_pdfs(work, dry_run=args.dry_run)
        print("\nMigration complete:")
        print(f"  Tars processed:  {stats['tars_processed']}")
        print(f"  Tars skipped:    {stats['tars_skipped']}")
        print(f"  Tars failed:     {stats['tars_failed']}")
        print(f"  PDFs copied:     {stats['pdfs_copied']}")
        print(f"  PDFs failed:     {stats['pdfs_failed']}")
        print(f"  Milvus updated:  {stats['milvus_updated']}")

    elif args.command == "status":
        show_status(INDEX_DB_PATH)


if __name__ == "__main__":
    main()
