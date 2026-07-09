#!/usr/bin/env python3
"""
parse_to_markdown.py — 批量 PDF → Markdown（多worker分片版）

分片策略:
  hash(arxiv_id) % SHARD_COUNT == SHARD_ID → 本机处理
  每台机器独立 checkpoint，互不干扰。

worker 只做纯计算（PDF → markdown 字符串），不碰 Milvus/gRPC/文件写入。
主进程集中处理：写文件、更新 Milvus、checkpoint、日志。日志全部由主进程
输出，无并发交错问题。

用法:
  # 单机 4 worker
  uv run python scripts/parse_to_markdown.py --workers 4 --max-rows 10000

  # 分布式 — 16 台机器，本机是 #0
  PARSE_MD_SHARD_ID=0 PARSE_MD_SHARD_COUNT=16 uv run python scripts/parse_to_markdown.py --workers 4

  # 断点续传
  uv run python scripts/parse_to_markdown.py --workers 4

  # 清空 checkpoint 重新跑
  uv run python scripts/parse_to_markdown.py --workers 4 --delete-ckpt
"""

from __future__ import annotations

import argparse
import hashlib
import multiprocessing
import os
import signal
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from compass.logging import configure_logging  # noqa: E402
from compass.storage import storage  # noqa: E402


# ── Shard helper ────────────────────────────────────────────────────────────


def _shard_of(aid: str, count: int) -> int:
    """Deterministic, cross-machine stable shard index (SHA256 based)."""
    digest = hashlib.sha256(aid.encode()).digest()
    return int.from_bytes(digest[:8], "big") % count


# ── Worker result ───────────────────────────────────────────────────────────


@dataclass
class WorkerResult:
    arxiv_id: str
    success: bool
    markdown: str | None = None
    # ── timing ──
    elapsed_ms: float = 0.0
    # ── input metrics ──
    pdf_bytes: int = 0  # file size in bytes
    pdf_pages: int = 0  # page count (0 = couldn't read)
    # ── output metrics ──
    md_chars: int = 0
    # ── error detail (when not success) ──
    error: str = ""
    error_type: str = ""
    error_tb: str = ""


# ── Worker function (top-level, picklable for spawn) ────────────────────────


def _process_one(args: tuple[str, Path, bool]) -> WorkerResult:
    """Pure computation: read PDF → markdown string.

    Delegates to ``compass.pipeline.pdf_md.pdf_to_markdown``.
    No Milvus, no file writes, no logging — everything goes into WorkerResult.

    Args:
        args: (arxiv_id, pdf_path, fast) — fast=True uses pymupdf.get_text()
              (~0.05s/paper), fast=False uses pymupdf4llm (~20s/paper).
    """
    from compass.pipeline.pdf_md import PDFMdError, pdf_to_markdown  # noqa: E402

    aid, pdf_path, fast = args
    t0 = time.monotonic()

    try:
        if not pdf_path.is_file():
            return WorkerResult(
                arxiv_id=aid,
                success=False,
                error=f"PDF file not found: {pdf_path}",
                error_type="FileNotFoundError",
                elapsed_ms=(time.monotonic() - t0) * 1000,
            )

        # File size + page count (fast metadata, pymupdf is a transitive dep)
        pdf_bytes = pdf_path.stat().st_size
        pdf_pages = _count_pdf_pages(pdf_path)

        # ── Convert ──
        md = pdf_to_markdown(pdf_path, fast=fast)
        elapsed_ms = (time.monotonic() - t0) * 1000

        return WorkerResult(
            arxiv_id=aid,
            success=True,
            markdown=md,
            elapsed_ms=elapsed_ms,
            pdf_bytes=pdf_bytes,
            pdf_pages=pdf_pages,
            md_chars=len(md),
        )

    except PDFMdError as exc:
        elapsed_ms = (time.monotonic() - t0) * 1000
        return WorkerResult(
            arxiv_id=aid,
            success=False,
            error=f"PDFMdError: {exc}",
            error_type="PDFMdError",
            error_tb=traceback.format_exc(),
            elapsed_ms=elapsed_ms,
        )
    except Exception as exc:
        elapsed_ms = (time.monotonic() - t0) * 1000
        return WorkerResult(
            arxiv_id=aid,
            success=False,
            error=f"{type(exc).__name__}: {exc}",
            error_type=type(exc).__name__,
            error_tb=traceback.format_exc(),
            elapsed_ms=elapsed_ms,
        )


def _count_pdf_pages(pdf_path: Path) -> int:
    """Fast page count via pymupdf metadata read (no rendering)."""
    try:
        import pymupdf

        doc = pymupdf.open(str(pdf_path))
        n = len(doc)
        doc.close()
        return n
    except Exception:
        return 0


# ── Stats (main-process only — no locking needed) ───────────────────────────


@dataclass
class Stats:
    scanned: int = 0
    ok: int = 0
    fail: int = 0
    skipped_done: int = 0
    skipped_shard: int = 0
    skipped_no_created: int = 0
    start_ts: float = field(default_factory=time.monotonic)
    # accumulators for aggregate metrics
    total_elapsed_ms: float = 0.0
    total_pdf_bytes: int = 0
    total_pdf_pages: int = 0
    total_md_chars: int = 0

    def elapsed(self) -> float:
        return time.monotonic() - self.start_ts

    def rate(self) -> float:
        e = self.elapsed()
        return self.ok / e if e > 0 else 0

    def avg_ms(self) -> float:
        return self.total_elapsed_ms / self.ok if self.ok > 0 else 0

    def summary(self) -> str:
        elapsed_h = self.elapsed() / 3600
        avg_ms = self.avg_ms()
        return (
            f"scanned={self.scanned:,}  ok={self.ok:,}  fail={self.fail:,}  "
            f"skip(done)={self.skipped_done:,}  skip(shard)={self.skipped_shard:,}  "
            f"skip(no_created)={self.skipped_no_created:,}  "
            f"elapsed={elapsed_h:.1f}h  rate={self.rate():.2f}/s  avg={avg_ms:.0f}ms"
        )


# ── Checkpoint ──────────────────────────────────────────────────────────────


class Checkpoint:
    def __init__(self, ckpt_dir: Path, suffix: str) -> None:
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        self._done_file = ckpt_dir / f"done_ids{suffix}.txt"
        self._failed_file = ckpt_dir / f"failed_ids{suffix}.txt"
        self.done: set[str] = self._load(self._done_file)
        self.failed: set[str] = self._load(self._failed_file)

    @staticmethod
    def _load(p: Path) -> set[str]:
        if not p.exists():
            return set()
        return {line.strip() for line in p.read_text().splitlines() if line.strip()}

    def mark_done(self, aid: str) -> None:
        with open(self._done_file, "a") as f:
            f.write(f"{aid}\n")
        self.done.add(aid)

    def mark_failed(self, aid: str) -> None:
        with open(self._failed_file, "a") as f:
            f.write(f"{aid}\n")
        self.failed.add(aid)


# ── Support — _update_one ───────────────────────────────────────────────────


def _update_one(aid: str, log: Any) -> bool:
    """Update has_markdown=True in Milvus with retry.  Returns True on success."""
    from compass.store.ingest import update_arxiv_paper  # noqa: F811

    for attempt in range(1, 4):
        try:
            update_arxiv_paper(aid, {"has_markdown": True})
            return True
        except Exception:
            if attempt < 3:
                log.warning("milvus_update_retry", arxiv_id=aid, attempt=attempt)
                time.sleep(0.5 * attempt)
            else:
                log.exception("milvus_update_failed", arxiv_id=aid)
    return False


# ── Shutdown flag ───────────────────────────────────────────────────────────

_shutdown_requested = False


def _on_shutdown(signum: int, _frame: object) -> None:
    global _shutdown_requested
    _shutdown_requested = True


# ── Main ────────────────────────────────────────────────────────────────────


def main() -> None:
    signal.signal(signal.SIGINT, _on_shutdown)
    signal.signal(signal.SIGTERM, _on_shutdown)

    # ── CLI ────────────────────────────────────────────────────────────
    shard_id = int(
        os.environ.get(
            "PARSE_MD_SHARD_ID", os.environ.get("SHARD_ID", os.environ.get("SLURM_PROCID", "0"))
        )
    )
    shard_count = int(
        os.environ.get(
            "PARSE_MD_SHARD_COUNT",
            os.environ.get("SHARD_COUNT", os.environ.get("SLURM_NTASKS", "1")),
        )
    )

    parser = argparse.ArgumentParser(
        epilog=f"shard: {shard_id}/{shard_count}  (env: PARSE_MD_SHARD_ID / PARSE_MD_SHARD_COUNT)"
    )
    parser.add_argument("--max-rows", type=int, default=0, help="最多处理论文数 (0=全量)")
    parser.add_argument(
        "--workers",
        type=int,
        default=min(32, os.cpu_count() or 4),
        help="并行 worker 数 (fast 模式下 IO 是瓶颈，默认最大32)",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        default=True,
        help="pymupdf get_text() 快速模式 (默认 / ~300x)",
    )
    parser.add_argument(
        "--slow", dest="fast", action="store_false", help="pymupdf4llm RAG 管线 (慢，layout-aware)"
    )
    parser.add_argument("--delete-ckpt", action="store_true", help="清空 checkpoint 重新跑")
    parser.add_argument("--log-level", type=str, default="INFO")
    args = parser.parse_args()

    # ── Logging ────────────────────────────────────────────────────────
    log_path = storage.log_path("parse_to_markdown", f"parse_to_markdown_shard{shard_id}.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    configure_logging(
        log_level=args.log_level,
        use_json=False,
        file_handler=(str(log_path), 200_000_000, 10),
        mode="w",
    )
    shard_label = f"{shard_id}/{shard_count}"
    log = structlog.get_logger("parse_to_markdown").bind(shard=shard_label)
    log.info(
        "started", workers=args.workers, shard_id=shard_id, shard_count=shard_count, fast=args.fast
    )

    # ── Checkpoint ─────────────────────────────────────────────────────
    ckpt_dir = storage.checkpoint_dir("parse_to_markdown")
    suffix = f"_{shard_id}of{shard_count}" if shard_count > 1 else ""

    if args.delete_ckpt:
        for tag in ("done_ids", "failed_ids"):
            f = ckpt_dir / f"{tag}{suffix}.txt"
            if f.exists():
                f.unlink()
        log.info("checkpoint_deleted")

    cp = Checkpoint(ckpt_dir, suffix)
    log.info("checkpoint_loaded", done=len(cp.done), failed=len(cp.failed))

    stats = Stats()

    # ── Chunk size for imap_unordered ───────────────────────────────────
    # fast mode: per-task cost ~0.05s, so IPC overhead dominates if chunksize=1.
    # Use larger chunksize to batch IPC, but not so large that we delay progress.
    chunksize = max(1, min(200, 10000 // args.workers))

    # ── Cursor-scan Milvus + process in batches ────────────────────────
    from compass.store.client import QUERY_CONSISTENCY, get_client  # noqa: E402

    client = get_client()
    log.info("gathering_start", max_rows=args.max_rows or "∞")

    batch_no = 0
    total_tasks: int = 0
    last_id: str = ""

    with multiprocessing.get_context("spawn").Pool(args.workers) as pool:
        while not _shutdown_requested:
            if args.max_rows and total_tasks >= args.max_rows:
                break

            # ── Scan one cursor batch ──────────────────────────────
            filt = (
                f"has_markdown == false and has_pdf == true and arxiv_id > '{last_id}'"
                if last_id
                else "has_markdown == false and has_pdf == true"
            )
            rows = client.query(
                "arxiv_papers",
                filter=filt,
                output_fields=["arxiv_id", "created"],
                consistency_level=QUERY_CONSISTENCY,
                limit=10000,
            )
            if not rows:
                break
            last_id = rows[-1]["arxiv_id"]

            # ── Filter: shard + checkpoint + validity ──────────────
            tasks: list[tuple[str, Path]] = []
            task_created: dict[str, str] = {}  # arxiv_id → created

            for r in rows:
                aid = r["arxiv_id"]
                # checkpoint skip
                if aid in cp.done or aid in cp.failed:
                    stats.skipped_done += 1
                    continue
                # shard filter
                if shard_count > 1 and _shard_of(aid, shard_count) != shard_id:
                    stats.skipped_shard += 1
                    continue
                created = r.get("created", "") or ""
                if not created:
                    stats.skipped_no_created += 1
                    continue
                pdf_path = storage.pdf_path(aid, created)
                tasks.append((aid, pdf_path, args.fast))
                task_created[aid] = created

            stats.scanned += len(rows)
            total_tasks += len(tasks)

            log.info(
                "gathering_batch",
                batch=batch_no,
                rows=len(rows),
                tasks=len(tasks),
                filtered_shard=stats.skipped_shard,
                filtered_done=stats.skipped_done,
                total_tasks=total_tasks,
            )

            if _shutdown_requested:
                break

            # ── Process batch with workers ─────────────────────────
            if not tasks:
                if len(rows) < 10000:
                    break
                batch_no += 1
                continue

            t_proc_start = time.monotonic()
            n_batch = len(tasks)

            log.info("batch_start", batch=batch_no, tasks=n_batch, workers=args.workers)

            # ── Process results as they arrive (streaming, not list-collect) ─
            # Iterative consumption avoids holding 10k markdown strings in memory.
            batch_ok = batch_fail = 0
            try:
                for r in pool.imap_unordered(_process_one, tasks, chunksize=chunksize):
                    if _shutdown_requested:
                        break

                    if r.success:
                        aid = r.arxiv_id
                        created = task_created[aid]
                        md_path = storage.markdown_path(aid, created)
                        try:
                            md_path.parent.mkdir(parents=True, exist_ok=True)
                            md_path.write_text(r.markdown or "", encoding="utf-8")
                        except OSError:
                            log.error("write_md_failed", arxiv_id=aid, path=str(md_path))
                            cp.mark_failed(aid)
                            stats.fail += 1
                            batch_fail += 1
                            continue

                        # Update Milvus (retry with backoff)
                        if not _update_one(aid, log):
                            # Milvus update failed after 3 retries —
                            # mark as failed so the paper is retried on next run
                            cp.mark_failed(aid)
                            stats.fail += 1
                            batch_fail += 1
                            continue
                        cp.mark_done(aid)
                        stats.ok += 1
                        batch_ok += 1
                        stats.total_elapsed_ms += r.elapsed_ms
                        stats.total_pdf_bytes += r.pdf_bytes
                        stats.total_pdf_pages += r.pdf_pages
                        stats.total_md_chars += r.md_chars

                        log.info(
                            "paper_ok",
                            arxiv_id=aid,
                            pages=r.pdf_pages,
                            pdf_kb=round(r.pdf_bytes / 1024, 1),
                            md_chars=r.md_chars,
                            elapsed_ms=round(r.elapsed_ms, 0),
                        )
                    else:
                        cp.mark_failed(r.arxiv_id)
                        stats.fail += 1
                        batch_fail += 1

                        log.warning(
                            "paper_fail",
                            arxiv_id=r.arxiv_id,
                            error_type=r.error_type,
                            error=r.error,
                            elapsed_ms=round(r.elapsed_ms, 0),
                        )
                        if r.error_tb:
                            log.debug("paper_fail_tb", arxiv_id=r.arxiv_id, traceback=r.error_tb)
            except Exception:
                log.exception("batch_processing_error", batch=batch_no)
                # Don't lose progress — already-written files are checkpointed

            t_proc_elapsed = time.monotonic() - t_proc_start
            batch_rate = n_batch / t_proc_elapsed if t_proc_elapsed > 0 else 0
            log.info(
                "batch_done",
                batch=batch_no,
                tasks=n_batch,
                ok=batch_ok,
                fail=batch_fail,
                elapsed_s=round(t_proc_elapsed, 1),
                rate=f"{batch_rate:.2f}/s",
                overall=stats.summary(),
            )

            batch_no += 1

            if args.max_rows and total_tasks >= args.max_rows:
                break
            if len(rows) < 10000:
                break

    # ── Final summary ──────────────────────────────────────────────────
    log.info("done", stats=stats.summary())
    if stats.ok:
        log.info(
            "aggregate_metrics",
            avg_ms=round(stats.avg_ms(), 0),
            total_pdf_gb=round(stats.total_pdf_bytes / 1e9, 2),
            total_pdf_pages=stats.total_pdf_pages,
            total_md_mb=round(stats.total_md_chars / 1e6, 2),
            pages_per_s=round(stats.total_pdf_pages / stats.elapsed(), 1),
        )

    print(f"\n  parse_to_markdown finished @ shard {shard_id}/{shard_count}")
    print(f"  {stats.summary()}")
    print(f"  workers: {args.workers}")
    print(f"  ckpt done:   {cp._done_file}  ({len(cp.done):,} entries)")
    print(f"  ckpt failed: {cp._failed_file}  ({len(cp.failed):,} entries)")


if __name__ == "__main__":
    main()
