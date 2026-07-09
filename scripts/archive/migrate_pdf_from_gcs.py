#!/usr/bin/env python3
"""Migrate PDFs from Kaggle GCS bucket to paper storage directories.

Fills the 2007-04 to 2018-12 gap not covered by arxiv_bulk.

Two-phase approach:
  Phase 2a — Gather: cursor-scan Milvus for has_pdf=False in 0704-1812.
  Phase 2b — Download: parallel gsutil cp from GCS, write to paper dirs,
              partial_update has_pdf=True in Milvus.

Usage:
  python scripts/migrate_pdf_from_gcs.py migrate        # Full migration
  python scripts/migrate_pdf_from_gcs.py migrate --dry-run  # Preview only
  python scripts/migrate_pdf_from_gcs.py status         # Show stats

Design:
  - gs://arxiv-dataset/arxiv/arxiv/pdf/{YYMM}/{ID}v1.pdf is the canonical path.
  - Milvus arxiv_id has no version suffix → try v1, v2, v3 in order.
  - 20-process multiprocessing Pool for GCS downloads (I/O bound).
  - SQLite checkpoint DB for idempotent resume at paper granularity.
  - All Milvus writes use partial_update=True — safe, no vector read.
"""

from __future__ import annotations

import argparse
import multiprocessing
import sqlite3
import subprocess
import time
from pathlib import Path

import structlog

from scholight.logging import configure_logging
from scholight.storage import storage
from scholight.store.client import _WRITE_LOCK, _resolve_token, _resolve_uri

# ── Constants ────────────────────────────────────────────────────────────────

# GCS public HTTP endpoint (free CDN bandwidth via Kaggle sponsorship).
# No gsutil needed — direct HTTPS download, zero child processes, zero memory leak.
GCS_BASE = "https://storage.googleapis.com/arxiv-dataset/arxiv/arxiv/pdf"
ID_RANGE_START = "0703.99999"
ID_RANGE_END = "1813"
GATHER_PAGE = 10000
CHECKPOINT_DB = Path(__file__).resolve().parent / "gcs_checkpoint.db"
DEFAULT_WORKERS = 60
MILVUS_UPDATE_BATCH = 100
PROGRESS_INTERVAL = 300  # seconds

# ── Logging ───────────────────────────────────────────────────────────────────

logger = structlog.get_logger(__name__)


# ── Phase 2a: Gather ─────────────────────────────────────────────────────────


def _gather() -> list[dict[str, str]]:
    """Cursor-scan Milvus for has_pdf=False papers in the GCS-covered range.

    Returns list of {arxiv_id, created} dicts, sorted by arxiv_id.
    """
    import pymilvus

    client = pymilvus.MilvusClient(uri=_resolve_uri(), token=_resolve_token(), timeout=30)
    papers: list[dict[str, str]] = []
    last_id = ID_RANGE_START
    total = 0
    t0 = time.monotonic()

    logger.info("gathering workload — has_pdf=false in GCS range")

    while True:
        rows = client.query(
            "arxiv_papers",
            filter=f"has_pdf == false and arxiv_id > '{last_id}' and arxiv_id < '{ID_RANGE_END}'",
            output_fields=["arxiv_id", "created"],
            limit=GATHER_PAGE,
        )
        if not rows:
            break
        for r in rows:
            papers.append({"arxiv_id": r["arxiv_id"], "created": r.get("created", "")})
            last_id = r["arxiv_id"]
            total += 1
        elapsed = time.monotonic() - t0
        logger.info("gather progress", scanned=total, elapsed_s=f"{elapsed:.0f}")

    elapsed = time.monotonic() - t0
    logger.info("gather complete", total=total, elapsed_s=f"{elapsed:.0f}")
    return papers


# ── Phase 2b: Download ───────────────────────────────────────────────────────

# curl -o handles HTTPS via OpenSSL (no Python-thread overhead).
# Each worker spawns exactly 1 curl child, waits for it, then moves on.
# This keeps the process tree flat — 60 workers = 60 curl at a time max.
# Compare: gsutil spawned 50+ subprocesses per worker → 3000+ zombie leak.


def _curl_download(url: str, dest: Path) -> bool:
    """Download PDF via /usr/bin/curl -o. Returns True on success."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [
                "curl",
                "-sS",
                "-o",
                str(dest),
                "--connect-timeout",
                "10",
                "--max-time",
                "120",
                "-w",
                "%{http_code}",
                url,
            ],
            capture_output=True,
            text=False,
            timeout=130,
        )
        if proc.returncode == 0 and dest.stat().st_size > 0:
            return True
        # If curl returns non-200 explicitly, log once
        if proc.stdout and proc.stdout.strip() not in ("200", ""):
            logger.debug("curl non-200", url=url[:80], http_code=proc.stdout.strip()[:10])
        if dest.exists():
            dest.unlink(missing_ok=True)
        return False
    except subprocess.TimeoutExpired:
        if dest.exists():
            dest.unlink(missing_ok=True)
        return False


def _download_one(args: tuple[int, dict[str, str], bool]) -> tuple[int, bool]:
    """Worker entry point. Downloads via curl (flat, no subprocess leak)."""
    idx, paper, dry_run = args
    arxiv_id = paper["arxiv_id"]
    created = paper["created"]

    if not created:
        logger.warning("no created date, skipping", arxiv_id=arxiv_id)
        return idx, False

    dest = storage.pdf_path(arxiv_id, created)
    if dest.exists() and dest.stat().st_size > 0:
        return idx, True

    if dry_run:
        return idx, False

    yymm = arxiv_id[:4]
    for version in range(1, 4):
        url = f"{GCS_BASE}/{yymm}/{arxiv_id}v{version}.pdf"
        if _curl_download(url, dest):
            return idx, True

    logger.warning("GCS PDF not found (any version)", arxiv_id=arxiv_id)
    return idx, False


# ── SQLite Checkpoint ────────────────────────────────────────────────────────


def _open_checkpoint(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS done (
            arxiv_id TEXT PRIMARY KEY
        )
    """)
    conn.commit()
    return conn


def _mark_done(conn: sqlite3.Connection, arxiv_id: str) -> None:
    conn.execute("INSERT OR IGNORE INTO done(arxiv_id) VALUES (?)", (arxiv_id,))
    conn.commit()


def _is_done(conn: sqlite3.Connection, arxiv_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM done WHERE arxiv_id = ?", (arxiv_id,)).fetchone()
    return row is not None


def _load_done_set(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT arxiv_id FROM done").fetchall()
    return {r[0] for r in rows}


# ── Milvus Batch Update ──────────────────────────────────────────────────────


def _batch_update_milvus(arxiv_ids: list[str]) -> int:
    """partial_update has_pdf=True for a batch of papers. Returns updated count."""
    if not arxiv_ids:
        return 0

    import pymilvus

    client = pymilvus.MilvusClient(uri=_resolve_uri(), token=_resolve_token(), timeout=30)
    total = 0
    for i in range(0, len(arxiv_ids), MILVUS_UPDATE_BATCH):
        batch = arxiv_ids[i : i + MILVUS_UPDATE_BATCH]
        batch_data = [{"arxiv_id": aid, "has_pdf": True} for aid in batch]
        try:
            with _WRITE_LOCK:
                result = client.upsert("arxiv_papers", data=batch_data, partial_update=True)
                total += result.get("upsert_count", len(batch))
        except Exception as exc:
            logger.error(
                "milvus partial update failed",
                batch_size=len(batch),
                first_id=batch[0],
                error=str(exc)[:200],
            )
    return total


# ── Orchestration ────────────────────────────────────────────────────────────


def migrate(dry_run: bool = False, workers: int = DEFAULT_WORKERS) -> dict[str, int]:
    """Run both phases with parallel workers and idempotent checkpoint."""
    # ── Gather ──
    papers = _gather()
    if not papers:
        logger.info("no papers to migrate")
        return {"total": 0, "downloaded": 0, "milvus_updated": 0}

    # ── Resume ──
    ck = _open_checkpoint(CHECKPOINT_DB)
    done = _load_done_set(ck)
    todo = [p for p in papers if p["arxiv_id"] not in done]
    logger.info(
        "workload ready",
        total=len(papers),
        done=len(done),
        todo=len(todo),
        dry_run=dry_run,
    )

    if not todo:
        logger.info("all papers already processed")
        ck.close()
        return {
            "total": len(papers),
            "downloaded": len(done),
            "milvus_updated": len(done),
        }

    # ── Progress tracking ──
    stats = {"total": len(papers), "downloaded": len(done), "milvus_updated": 0}
    t0 = time.monotonic()
    downloaded_batch: list[str] = []  # arxiv_ids pending Milvus update
    pending_ids: list[str] = []  # arxiv_ids that were downloaded but not yet in checkpoint

    # ── Parallel download ──
    max_idx = len(todo) - 1
    tasks = [(i, todo[i], dry_run) for i in range(len(todo))]

    with multiprocessing.Pool(processes=workers) as pool:
        # Use imap_unordered for progress visibility
        for idx, success in pool.imap_unordered(_download_one, tasks, chunksize=50):
            paper = todo[idx]
            aid = paper["arxiv_id"]

            if success:
                downloaded_batch.append(aid)
                pending_ids.append(aid)
                _mark_done(ck, aid)
                stats["downloaded"] += 1

            # Flush Milvus update periodically
            if len(downloaded_batch) >= MILVUS_UPDATE_BATCH:
                updated = _batch_update_milvus(downloaded_batch)
                stats["milvus_updated"] += updated
                downloaded_batch = []

            # Progress log
            elapsed = time.monotonic() - t0
            if elapsed >= PROGRESS_INTERVAL or idx >= max_idx:
                remaining = stats["total"] - stats["downloaded"]
                rate = stats["downloaded"] / elapsed if elapsed > 0 else 0
                eta = remaining / rate if rate > 0 else 0
                logger.info(
                    "migration progress",
                    total=stats["total"],
                    downloaded=stats["downloaded"],
                    milvus_updated=stats["milvus_updated"],
                    elapsed_m=f"{elapsed / 60:.1f}",
                    eta_m=f"{eta / 60:.1f}" if eta else "N/A",
                    rate_s=f"{rate:.1f}",
                )
                t0 = time.monotonic()

    # Final batch flush
    if downloaded_batch:
        updated = _batch_update_milvus(downloaded_batch)
        stats["milvus_updated"] += updated

    ck.close()
    logger.info("migration complete", **stats)
    return stats


# ── Status ───────────────────────────────────────────────────────────────────


def show_status() -> None:
    """Display GCS migration state."""
    import pymilvus

    client = pymilvus.MilvusClient(uri=_resolve_uri(), token=_resolve_token(), timeout=10)
    total = 0
    last_id = ID_RANGE_START
    while True:
        rows = client.query(
            "arxiv_papers",
            filter=f"has_pdf == false and arxiv_id > '{last_id}' and arxiv_id < '{ID_RANGE_END}'",
            output_fields=["arxiv_id"],
            limit=GATHER_PAGE,
        )
        if not rows:
            break
        total += len(rows)
        last_id = rows[-1]["arxiv_id"]
    logger.info("GCS range remaining", has_pdf_false=total)

    if CHECKPOINT_DB.exists():
        ck = _open_checkpoint(CHECKPOINT_DB)
        cnt = ck.execute("SELECT COUNT(*) FROM done").fetchone()[0]
        ck.close()
        logger.info("checkpoint DB", completed=cnt)
    else:
        logger.info("no checkpoint yet")


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate PDFs from Kaggle GCS (2007-04 to 2018-12 gap)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    m = sub.add_parser("migrate", help="Run migration")
    m.add_argument("--dry-run", action="store_true", help="Preview only")
    m.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Parallel workers")

    sub.add_parser("status", help="Show migration status")

    args = parser.parse_args()

    log_path = storage.log_path("migrate_gcs", "migrate_pdf_from_gcs.log")
    configure_logging(log_level="INFO", use_json=True, file_handler=(str(log_path), 50_000_000, 5))

    if args.command == "migrate":
        stats = migrate(dry_run=args.dry_run, workers=args.workers)
        print("\nMigration complete:")
        print(f"  Total papers:   {stats['total']}")
        print(f"  Downloaded:     {stats['downloaded']}")
        print(f"  Milvus updated: {stats['milvus_updated']}")

    elif args.command == "status":
        show_status()


if __name__ == "__main__":
    main()
