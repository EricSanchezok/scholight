#!/usr/bin/env python3
"""Download 07-26 orphan PDFs from Kaggle GCS via curl.  Stream-optimized.

Reads data/orphan_pdfs.txt line by line (no full load), filters to GCS
years (07-26), downloads with curl, partial_updates Milvus.

Usage:
  python scripts/gcs_fill_orphans.py              # full run
  python scripts/gcs_fill_orphans.py --dry-run    # first 200
"""

from __future__ import annotations

import argparse
import multiprocessing
import sqlite3
import subprocess
import time
from pathlib import Path

import structlog

from compass.logging import configure_logging
from compass.storage import storage
from compass.store.client import _WRITE_LOCK, _resolve_token, _resolve_uri

ORPHAN_FILE = Path(
    "/inspire/hdd/project/multi-agent/niexiaohang-25130061/academic-compass/data/orphan_pdfs.txt"
)
CHECKPOINT_DB = Path(__file__).resolve().parent / "gcs_orphan_checkpoint.db"
GCS_BASE = "https://storage.googleapis.com/arxiv-dataset/arxiv/arxiv/pdf"

logger = structlog.get_logger(__name__)


# ── Stream loader (no full-memory list) ─────────────────────────────────────


def _stream_orphans() -> list[tuple[str, str]]:
    """Stream-read orphan_pdfs.txt, filter to 07-26 GCS years."""
    orphans: list[tuple[str, str]] = []
    seen_dirs: set[str] = set()
    with open(ORPHAN_FILE) as f:
        f.readline()
        for line in f:
            line = line.strip()
            if not line or "\t" not in line:
                continue
            aid, dest = line.split("\t", 1)
            yy = aid[:2]
            if not yy.isdigit() or int(yy) < 7 or int(yy) > 26:
                continue
            orphan_dir = dest.rsplit("/", 1)[0]
            if orphan_dir in seen_dirs:
                continue
            seen_dirs.add(orphan_dir)
            orphans.append((aid, dest))
    logger.info("loaded GCS orphans", count=len(orphans))
    return orphans


# ── Download ────────────────────────────────────────────────────────────────


def _curl_download(arxiv_id: str, dest: str) -> bool:
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    yymm = arxiv_id[:4]
    for version in range(1, 4):
        url = f"{GCS_BASE}/{yymm}/{arxiv_id}v{version}.pdf"
        try:
            subprocess.run(
                [
                    "curl",
                    "-sS",
                    "-o",
                    str(dest_path),
                    "--connect-timeout",
                    "10",
                    "--max-time",
                    "60",
                    url,
                ],
                check=True,
                capture_output=True,
                timeout=65,
            )
            if dest_path.stat().st_size > 512:
                return True
            dest_path.unlink(missing_ok=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            dest_path.unlink(missing_ok=True)
    return False


def _download_one(args: tuple[int, str, str, bool]) -> tuple[int, bool]:
    idx, arxiv_id, dest, dry_run = args
    dest_path = Path(dest)
    if dest_path.exists() and dest_path.stat().st_size > 512:
        return idx, True
    if dry_run:
        return idx, False
    return idx, _curl_download(arxiv_id, dest)


# ── Checkpoint ──────────────────────────────────────────────────────────────


def _open_checkpoint(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS done (arxiv_id TEXT PRIMARY KEY)")
    conn.commit()
    return conn


# ── Milvus ──────────────────────────────────────────────────────────────────


def _batch_update(arxiv_ids: list[str]) -> int:
    import pymilvus

    client = pymilvus.MilvusClient(uri=_resolve_uri(), token=_resolve_token(), timeout=30)
    total = 0
    for i in range(0, len(arxiv_ids), 100):
        batch = arxiv_ids[i : i + 100]
        try:
            with _WRITE_LOCK:
                result = client.upsert(
                    "arxiv_papers",
                    data=[{"arxiv_id": a, "has_pdf": True} for a in batch],
                    partial_update=True,
                )
                total += result.get("upsert_count", len(batch))
        except Exception as exc:
            logger.error("batch", first=batch[0], error=str(exc)[:200])
    return total


# ── Main ────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int, default=60)
    args = parser.parse_args()

    log_path = storage.log_path("gcs_orphan", "gcs_fill_orphans.log")
    configure_logging(log_level="INFO", use_json=True, file_handler=(str(log_path), 100_000_000, 5))

    all_orphans = _stream_orphans()
    ck = _open_checkpoint(CHECKPOINT_DB)
    done = {r[0] for r in ck.execute("SELECT arxiv_id FROM done").fetchall()}
    todo = [x for x in all_orphans if x[0] not in done]
    logger.info("workload", total=len(all_orphans), done=len(done), todo=len(todo))

    if args.dry_run:
        todo = todo[:200]

    if not todo:
        logger.info("nothing to do")
        ck.close()
        return

    tasks = [(i, aid, dest, args.dry_run) for i, (aid, dest) in enumerate(todo)]
    max_idx = len(todo) - 1
    downloaded: list[str] = []
    stats = {"total": len(all_orphans), "ok": len(done), "failed": 0, "milvus": 0}
    t0 = time.monotonic()

    with multiprocessing.Pool(processes=args.workers) as pool:
        for idx, ok in pool.imap_unordered(_download_one, tasks, chunksize=50):
            aid = todo[idx][0]
            if ok:
                ck.execute("INSERT OR IGNORE INTO done(arxiv_id) VALUES (?)", (aid,))
                ck.commit()
                downloaded.append(aid)
                stats["ok"] += 1
            else:
                stats["failed"] += 1

            if len(downloaded) >= 100:
                stats["milvus"] += _batch_update(downloaded)
                downloaded = []

            elapsed = time.monotonic() - t0
            if elapsed >= 120 or idx >= max_idx:
                remaining = stats["total"] - stats["ok"]
                rate = (stats["ok"] - len(done)) / elapsed if elapsed > 0 else 0
                logger.info(
                    "progress",
                    ok=stats["ok"],
                    failed=stats["failed"],
                    milvus=stats["milvus"],
                    elapsed_m=f"{elapsed / 60:.1f}",
                    rate_s=f"{rate:.1f}",
                    eta_m=f"{remaining / rate / 60:.0f}" if rate > 0 else "N/A",
                )
                t0 = time.monotonic()

    if downloaded:
        stats["milvus"] += _batch_update(downloaded)
    ck.close()
    logger.info("complete", **stats)
    print(f"OK={stats['ok']} Failed={stats['failed']} Milvus={stats['milvus']}")


if __name__ == "__main__":
    main()
