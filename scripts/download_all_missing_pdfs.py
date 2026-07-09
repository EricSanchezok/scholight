#!/usr/bin/env python3
"""Download missing PDFs from export.arxiv.org + Kaggle GCS.

Uses store 模块专业方法（update_arxiv_paper / count_papers_without），
绝不清空任何字段。

三态 checkpoint（SQLite）:
  - done:    下载成功 ✅
  - no_pdf:  arXiv 返回 404，该论文确实没有 PDF（不会自动重试）
  - failed:  其他失败（会通过 --retry-failed 重试）

限流控制：
  - 默认 10 并发（export.arxiv.org 可接受的持续负载）
  - 429/503 自动退避 0.5s→2s→8s 重试
  - 限流/超时/curl错误不会写 failed checkpoint → 下次 run 自动重试

用法：
  python scripts/download_all_missing_pdfs.py run              # 正式跑
  python scripts/download_all_missing_pdfs.py run --workers 20 # 调并发
  python scripts/download_all_missing_pdfs.py run --retry-failed
  python scripts/download_all_missing_pdfs.py status
"""

from __future__ import annotations

import argparse
import multiprocessing
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import structlog

from compass.logging import configure_logging
from compass.storage import storage
from compass.store.ingest import update_arxiv_paper

# ── Constants ──────────────────────────────────────────────────────────────────

EXPORT_BASE = "https://export.arxiv.org"
GCS_BASE = "https://storage.googleapis.com/arxiv-dataset/arxiv/arxiv/pdf"

GATHER_BATCH = 10000
CHECKPOINT_DB = Path(__file__).resolve().parent / "export_dl_checkpoint.db"
FAILED_LOG = Path(__file__).resolve().parent.parent / "data" / "export_dl_failed.txt"
DEFAULT_WORKERS = 10  # safe for continuous export.arxiv.org access
UPDATE_BATCH = 100
PROGRESS_INTERVAL = 120
MIN_DISK_GB = 60

# Retry: transient errors (rate-limit, timeout) get retried, then skipped (not checkpointed)
RETRY_BACKOFF = [0.5, 2.0, 8.0]

# ── Logging ────────────────────────────────────────────────────────────────────

logger = structlog.get_logger(__name__)


# ── Gather workload ────────────────────────────────────────────────────────────


def _gather_work() -> list[dict[str, str]]:
    """Cursor-scan all has_pdf=False papers from Milvus."""
    from compass.store.client import get_client

    client = get_client()
    papers: list[dict[str, str]] = []
    last_id = ""
    total = 0
    t0 = time.monotonic()
    logger.info("gathering has_pdf=false papers")

    while True:
        filt = f"has_pdf == false and arxiv_id > '{last_id}'" if last_id else "has_pdf == false"
        rows = client.query(
            "arxiv_papers",
            filter=filt,
            output_fields=["arxiv_id", "created"],
            limit=GATHER_BATCH,
            timeout=30,
        )
        if not rows:
            break
        for r in rows:
            papers.append({"arxiv_id": r["arxiv_id"], "created": r.get("created", "")})
            last_id = r["arxiv_id"]
            total += 1
        if total % 50000 == 0:
            logger.info("gather progress", scanned=total)

    logger.info("gather complete", total=total, elapsed_s=f"{time.monotonic() - t0:.1f}")
    return papers


# ── PDF verification ──────────────────────────────────────────────────────────


def _pdf_ok(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 512:
        return False
    try:
        with open(path, "rb") as fh:
            return fh.read(5).startswith(b"%PDF")
    except OSError:
        return False


# ── Download ───────────────────────────────────────────────────────────────────


def _gcs_url(arxiv_id: str) -> str | None:
    """GCS URL for 2007-2018 papers (0704.xxxxx - 1812.xxxxx)."""
    if "/" not in arxiv_id and len(arxiv_id) >= 4:
        try:
            y = int(arxiv_id[:2])
            if 7 <= y <= 18:
                return f"{GCS_BASE}/{arxiv_id[:4]}/{arxiv_id}v1.pdf"
        except ValueError:
            pass
    return None


def _curl_get(url: str, dest: Path, timeout: int) -> str | None:
    """Download via curl. Returns None on success, error string on failure."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [
                "curl",
                "-sSL",
                "-o",
                str(dest),
                "--connect-timeout",
                "15",
                "--max-time",
                str(timeout),
                "-w",
                "%{http_code}",
                url,
            ],
            capture_output=True,
            text=False,
            timeout=timeout + 10,
        )
        code_b = proc.stdout.strip() if proc.stdout else b""
        code = code_b.decode() if isinstance(code_b, bytes) else str(code_b)

        if proc.returncode == 0 and _pdf_ok(dest):
            return None  # success

        if dest.exists():
            dest.unlink(missing_ok=True)

        if code == "404":
            return "404"
        elif code in ("429", "503"):
            return "rate_limit"
        elif code and code != "200":
            return f"http_{code}"
        elif proc.returncode != 0:
            return f"curl_{proc.returncode}"
        else:
            return "invalid_pdf"

    except subprocess.TimeoutExpired:
        if dest.exists():
            dest.unlink(missing_ok=True)
        return "timeout"


def _download_one(args: tuple[int, dict[str, str], bool]) -> tuple[int, bool | str]:
    """Worker: download one PDF.

    Returns (idx, status):
      - True  → success, mark done
      - "404" → paper has no PDF → mark no_pdf
      - False → transient failure (rate-limit/timeout) → skip (not checkpointed)
    """
    idx, paper, dry_run = args
    aid = paper["arxiv_id"]
    created = paper["created"]

    if not created:
        return idx, "404"  # can't compute path

    dest = storage.pdf_path(aid, created)

    # Tier 1: already on disk
    if _pdf_ok(dest):
        return idx, True

    if dry_run:
        return idx, False

    # Tier 2: Kaggle GCS (2007-2018)
    url = _gcs_url(aid)
    if url:
        err = _curl_get(url, dest, timeout=30)
        if err is None:
            return idx, True
        if err == "404":
            pass  # fall through to export.arxiv.org — sometimes old IDs aren't on GCS

    # Tier 3: export.arxiv.org with retry
    export_url = f"{EXPORT_BASE}/pdf/{aid}.pdf"
    for attempt, wait in enumerate(RETRY_BACKOFF):
        err = _curl_get(export_url, dest, timeout=120)
        if err is None:
            return idx, True
        if err == "404":
            return idx, "404"  # permanent: paper has no PDF
        if err in ("rate_limit", "timeout"):
            # Transient — retry with backoff
            time.sleep(wait)
            continue
        # Other failure (invalid pdf, curl error) — retry once then give up
        if attempt < len(RETRY_BACKOFF) - 1:
            time.sleep(wait)
            continue
        break

    # After all retries exhausted — transient failure
    return idx, False


# ── Checkpoint (SQLite) ───────────────────────────────────────────────────────


def _open_ck(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("CREATE TABLE IF NOT EXISTS done (arxiv_id TEXT PRIMARY KEY, created TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS no_pdf (arxiv_id TEXT PRIMARY KEY, created TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS failed (arxiv_id TEXT PRIMARY KEY, created TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS metrics (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()
    return conn


def _ck_sets(conn: sqlite3.Connection) -> tuple[set[str], set[str], set[str]]:
    """Return (done, no_pdf, failed) — all papers already handled."""
    done = {r[0] for r in conn.execute("SELECT arxiv_id FROM done").fetchall()}
    no_pdf = {r[0] for r in conn.execute("SELECT arxiv_id FROM no_pdf").fetchall()}
    failed = {r[0] for r in conn.execute("SELECT arxiv_id FROM failed").fetchall()}
    return done, no_pdf, failed


def _mark(conn: sqlite3.Connection, table: str, aid: str, created: str = "") -> None:
    conn.execute(f"INSERT OR IGNORE INTO {table}(arxiv_id, created) VALUES (?, ?)", (aid, created))
    conn.commit()


# ── Milvus Update ──────────────────────────────────────────────────────────────


def _batch_update_has_pdf(arxiv_ids: list[str]) -> int:
    """Set has_pdf=True via update_arxiv_paper. Safe — never touches other fields."""
    ok = 0
    for aid in arxiv_ids:
        try:
            if update_arxiv_paper(aid, {"has_pdf": True}):
                ok += 1
        except Exception:
            # Fallback: include ARRAY fields for pymilvus 3.0 + Milvus 2.6 compat
            try:
                from compass.store.client import get_client, _WRITE_LOCK

                client = get_client()
                rows = client.query(
                    "arxiv_papers",
                    filter=f'arxiv_id == "{aid}"',
                    output_fields=["authors", "categories", "updated_history"],
                    limit=1,
                    timeout=10,
                )
                if rows:
                    extra = {"has_pdf": True}
                    for f in ("authors", "categories", "updated_history"):
                        val = rows[0].get(f)
                        if val is not None:
                            extra[f] = (
                                list(val)
                                if hasattr(val, "__iter__") and not isinstance(val, str)
                                else val
                            )
                    with _WRITE_LOCK:
                        r = client.upsert(
                            "arxiv_papers",
                            data=[{"arxiv_id": aid, **extra}],
                            partial_update=True,
                            consistency_level="Strong",
                        )
                        if r.get("upsert_count", 0):
                            ok += 1
            except Exception:
                pass
    return ok


# ── Signal handling ──────────────────────────────────────────────────────────


class GracefulExiter:
    def __init__(self) -> None:
        self._flag = False
        signal.signal(signal.SIGINT, self._handler)
        signal.signal(signal.SIGTERM, self._handler)

    def _handler(self, signum: int, _frame: object) -> None:
        if self._flag:
            sys.exit(1)
        self._flag = True
        logger.warning("SIGINT — stopping after in-flight work")

    def __bool__(self) -> bool:
        return bool(self._flag)


# ── Disk space ────────────────────────────────────────────────────────────────


def _check_disk() -> None:
    import shutil

    free = shutil.disk_usage(storage.root).free / (1024**3)
    if free < MIN_DISK_GB:
        logger.warning("low disk", free_gb=f"{free:.1f}")
        print(f"⚠  Low disk: {free:.1f} GB free (≥ {MIN_DISK_GB} GB recommended)")
    else:
        logger.info("disk OK", free_gb=f"{free:.1f}")


# ── Build todo ────────────────────────────────────────────────────────────────


def _build_todo(
    papers: list[dict[str, str]], retry_failed: bool, conn: sqlite3.Connection
) -> list[dict[str, str]]:
    done, no_pdf, failed = _ck_sets(conn)
    excluded = done | no_pdf

    if retry_failed:
        todo = [p for p in papers if p["arxiv_id"] in failed]
        logger.info("retry mode", to_retry=len(todo))
        conn.execute("DELETE FROM failed")
        conn.commit()
    else:
        todo = [p for p in papers if p["arxiv_id"] not in excluded]
        logger.info(
            "workload",
            total=len(papers),
            done=len(done),
            no_pdf=len(no_pdf),
            failed=len(failed),
            todo=len(todo),
        )
    return todo


# ── Main run ──────────────────────────────────────────────────────────────────


def run(
    dry_run: bool = False, workers: int = DEFAULT_WORKERS, retry_failed: bool = False
) -> dict[str, int]:
    exiter = GracefulExiter()
    _check_disk()

    papers = _gather_work()
    if not papers:
        return {"total": 0, "downloaded": 0, "no_pdf": 0, "transient": 0, "milvus_updated": 0}

    ck = _open_ck(CHECKPOINT_DB)
    todo = _build_todo(papers, retry_failed, ck)
    if not todo:
        ck.close()
        return {
            "total": len(papers),
            "downloaded": len(papers),
            "no_pdf": 0,
            "transient": 0,
            "milvus_updated": len(papers),
        }

    if dry_run:
        todo = todo[:500]

    total_target = len(todo)
    stats = {
        "total": len(papers),
        "downloaded": 0,
        "no_pdf": 0,
        "transient": 0,
        "milvus_updated": 0,
    }
    t0 = time.monotonic()
    last_progress = t0
    pending_update: list[str] = []
    tasks = [(i, todo[i], dry_run) for i in range(len(todo))]

    with multiprocessing.Pool(processes=workers) as pool:
        try:
            for idx, outcome in pool.imap_unordered(_download_one, tasks, chunksize=20):
                if exiter:
                    break

                paper = todo[idx]
                aid = paper["arxiv_id"]
                created = paper.get("created", "")

                if outcome is True:
                    if not dry_run:
                        _mark(ck, "done", aid)
                    pending_update.append(aid)
                    stats["downloaded"] += 1

                elif outcome == "404":
                    if not dry_run:
                        _mark(ck, "no_pdf", aid, created)
                    stats["no_pdf"] += 1

                else:
                    # Transient: NOT checkpointed — will retry on next run
                    stats["transient"] += 1

                if len(pending_update) >= UPDATE_BATCH:
                    if not dry_run:
                        stats["milvus_updated"] += _batch_update_has_pdf(pending_update)
                    pending_update = []

                # Progress
                now = time.monotonic()
                elapsed = now - t0
                if now - last_progress >= PROGRESS_INTERVAL or idx >= len(todo) - 1:
                    done_s = stats["downloaded"]
                    rate = done_s / elapsed if elapsed > 0 else 0
                    remaining = total_target - done_s - stats["no_pdf"]
                    logger.info(
                        "progress",
                        done=done_s,
                        no_pdf=stats["no_pdf"],
                        transient=stats["transient"],
                        total=total_target,
                        milvus=stats["milvus_updated"],
                        success_pct=f"{done_s / max(done_s + stats['no_pdf'] + stats['transient'], 1) * 100:.1f}",
                        elapsed_h=f"{elapsed / 3600:.2f}",
                        eta_h=f"{remaining / rate / 3600:.1f}" if rate > 0 else "?",
                        rate_s=f"{rate:.2f}",
                    )
                    last_progress = now

        except KeyboardInterrupt:
            logger.warning("interrupt")
        except Exception as e:
            logger.exception("unexpected error")
        finally:
            pool.terminate()
            pool.join()

    if pending_update and not dry_run:
        stats["milvus_updated"] += _batch_update_has_pdf(pending_update)

    if not dry_run:
        dur = int(time.monotonic() - t0)
        conn = _open_ck(CHECKPOINT_DB)
        for k, v in (
            ("downloaded", stats["downloaded"]),
            ("no_pdf", stats["no_pdf"]),
            ("transient", stats["transient"]),
            ("milvus", stats["milvus_updated"]),
            ("duration_s", dur),
        ):
            conn.execute("INSERT OR REPLACE INTO metrics(key, value) VALUES (?, ?)", (k, str(v)))
        conn.commit()
        conn.close()

    ck.close()

    if stats["no_pdf"] > 0 and not dry_run:
        FAILED_LOG.parent.mkdir(parents=True, exist_ok=True)
        ck2 = _open_ck(CHECKPOINT_DB)
        no_pdf_set = {r[0] for r in ck2.execute("SELECT arxiv_id FROM no_pdf").fetchall()}
        ck2.close()
        with open(FAILED_LOG, "w") as f:
            f.write("# arxiv_id\tcreated\tstatus\n")
            for p in papers:
                if p["arxiv_id"] in no_pdf_set:
                    f.write(f"{p['arxiv_id']}\t{p.get('created', '')}\tno_pdf\n")
        logger.info("no_pdf list saved", path=str(FAILED_LOG), count=len(no_pdf_set))

    logger.info("run complete", **stats)
    return stats


# ── Status ────────────────────────────────────────────────────────────────────


def show_status() -> None:
    from compass.store.ingest import count_papers_without

    remaining = count_papers_without("has_pdf")
    print(f"\n{'=' * 50}")
    print(f"  Milvus has_pdf=False:  {remaining:>10,}")
    print(f"{'=' * 50}")

    if CHECKPOINT_DB.exists():
        ck = _open_ck(CHECKPOINT_DB)
        done = ck.execute("SELECT COUNT(*) FROM done").fetchone()[0]
        no_pdf = ck.execute("SELECT COUNT(*) FROM no_pdf").fetchone()[0]
        failed = ck.execute("SELECT COUNT(*) FROM failed").fetchone()[0]
        metrics = dict(ck.execute("SELECT key, value FROM metrics").fetchall())
        ck.close()
        print(f"  Checkpoint done:      {done:>10,}")
        print(f"  Checkpoint no_pdf:    {no_pdf:>10,}")
        print(f"  Checkpoint failed:    {failed:>10,}")
        print(f"  Remaining (net):      {max(0, remaining - done):>10,}")
        print()
        if metrics:
            print(f"  Last run:")
            for k in ("downloaded", "no_pdf", "transient", "milvus"):
                if k in metrics:
                    print(f"    {k:12s}  {int(metrics[k]):>8,}")
            ds = int(metrics.get("duration_s", 0))
            print(f"    {'duration':12s}  {ds // 3600}h {(ds % 3600) // 60}m")
    else:
        print("  Checkpoint:           none")
    print(f"{'=' * 50}\n")


# ── CLI ────────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Download missing PDFs from arXiv")
    sub = parser.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="Run download (resumes from checkpoint)")
    r.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    r.add_argument("--retry-failed", action="store_true")

    sub.add_parser("status", help="Show state")

    args = parser.parse_args()
    log_path = storage.log_path("export_dl", "download_all_missing_pdfs.log")
    configure_logging(log_level="INFO", use_json=True, file_handler=(str(log_path), 100_000_000, 5))

    if args.command == "run":
        s = run(workers=args.workers, retry_failed=args.retry_failed)
        print(f"\n{'=' * 50}")
        for k in ("total", "downloaded", "no_pdf", "transient", "milvus_updated"):
            print(f"  {k:20s}  {s.get(k, 0):>8,}")
        print(f"{'=' * 50}")
        if s.get("no_pdf"):
            print(f"  📄 {s['no_pdf']} papers have no PDF on arXiv (404)")
        if s.get("transient"):
            print(f"  🔄 {s['transient']} transient failures — will auto-retry next run")
        print()

    elif args.command == "status":
        show_status()


if __name__ == "__main__":
    main()
