#!/usr/bin/env python3
"""
ingest_chunks.py — 批量 Chunk → Embedding → Sparse → 入库（单进程/多Worker）

Pipeline:
  has_markdown=True AND has_chunks=False 的论文
  → 读 paper.md → chunk_markdown (detect latex/pdf)
  → 批量 Dense Embedding (Qwen3-Embed-0.6B, batch=512, 8并发)
  → 批量 Sparse Encode (BM25 模型，每worker加载一次)
  → 论文级 safe_insert：按 chunk_idx 定点删除 → 再 insert
  → update_arxiv_paper(has_chunks=True)
  → checkpoint（append + fsync）

Checkpoint: append-only，POSIX 保证单行原子性，多 worker 并发写安全。

用法:
  uv run python scripts/ingest_chunks.py --max-rows 1000            # 测试
  uv run python scripts/ingest_chunks.py --num-workers 4            # 4 worker
  uv run python scripts/ingest_chunks.py --num-workers 8 --max-rows 5000  # 大测试
  nohup uv run python scripts/ingest_chunks.py --num-workers 8 > /dev/null 2>&1 &
  uv run python scripts/ingest_chunks.py --delete-ckpt             # 清空 checkpoint
"""

from __future__ import annotations

import argparse
import asyncio
import multiprocessing as mp
import os
import re
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

# ── Constants ────────────────────────────────────────────────────────────────

BATCH_SIZE = 50  # 每批论文数（单worker = micro-batch，多worker = 每个 task）
SCAN_BATCH = 10000  # 每轮从 Milvus 扫的行数
EMBED_BATCH = 512  # 单次 embedding API 的文本数
SPARSE_BATCH = 1024  # 单次 sparse encode 的文本数
LOG_ROTATION_BYTES = 200_000_000
LOG_BACKUP_COUNT = 10
MD_MAX_LEN = 16384
TITLE_MAX_LEN = 1024

_RE_YAML = re.compile(r"^---\n")


# ── Data structures ──────────────────────────────────────────────────────────


@dataclass
class Stats:
    scanned: int = 0
    done: int = 0
    fail: int = 0
    skip_done: int = 0
    start_ts: float = field(default_factory=time.monotonic)
    total_chunks: int = 0

    def elapsed(self) -> float:
        return time.monotonic() - self.start_ts

    def rate(self) -> float:
        e = self.elapsed()
        return self.done / e if e > 0 else 0

    def summary(self) -> str:
        h = self.elapsed() / 3600
        return (
            f"scanned={self.scanned:,}  done={self.done:,}  fail={self.fail:,}  "
            f"skip_done={self.skip_done:,}  chunks={self.total_chunks:,}  "
            f"elapsed={h:.1f}h  rate={self.rate():.2f}/s"
        )


@dataclass
class BatchResult:
    done: int = 0
    fail: int = 0
    chunks: int = 0
    error: str = ""


# ── Checkpoint ──────────────────────────────────────────────────────────────


class Checkpoint:
    """断点续传。append + flush，多 worker 并发写安全（POSIX 行级原子）。"""

    def __init__(self, ckpt_dir: Path) -> None:
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        self._done_file = ckpt_dir / "done_ids.txt"
        self._failed_file = ckpt_dir / "failed_ids.txt"
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
            f.flush()
            os.fsync(f.fileno())
        self.done.add(aid)

    def mark_failed(self, aid: str) -> None:
        with open(self._failed_file, "a") as f:
            f.write(f"{aid}\n")
            f.flush()
            os.fsync(f.fileno())
        self.failed.add(aid)


# ── Markdown source detection ────────────────────────────────────────────────


def _detect_source(md_text: str) -> str:
    return "latex" if _RE_YAML.search(md_text) else "pdf"


# ── Chunk processing for one paper ─────────────────────────────────────────


def _process_one_paper(aid: str, created: str) -> tuple[list[dict], str | None]:
    """读 paper.md → chunk_markdown。若MD文件缺失则用PDF fast模式补齐。"""
    from compass.pipeline.chunkers.md_chunker import chunk_markdown
    from compass.pipeline.pdf_md import pdf_to_markdown as _pdf_to_md

    md_path = storage.markdown_path(aid, created)

    if not md_path.is_file():
        # ── Fallback: 用 PDF fast 模式快速生成 markdown ──────────────
        pdf_path = storage.pdf_path(aid, created)
        if not pdf_path.is_file():
            return [], f"No MD and no PDF for {aid}"
        try:
            md_text = _pdf_to_md(str(pdf_path), fast=True)
        except Exception as exc:
            return [], f"PDF fast conversion failed: {exc}"
        # 写回磁盘，下次直接命中
        try:
            md_path.parent.mkdir(parents=True, exist_ok=True)
            md_path.write_text(md_text, encoding="utf-8")
        except Exception as exc:
            return [], f"Failed to write MD after fast conversion: {exc}"
        source = "pdf"
    else:
        try:
            md_text = md_path.read_text(encoding="utf-8")
        except Exception as exc:
            return [], f"Failed to read MD: {exc}"
        source = _detect_source(md_text)

    try:
        chunks = chunk_markdown(md_text, source=source)
    except Exception as exc:
        return [], f"Chunking failed ({source}): {exc}"

    if not chunks:
        return [], "No chunks produced"
    if len(chunks) == 1 and not chunks[0].content.strip():
        return [], "Only chunk is empty"

    result: list[dict] = []
    for c in chunks:
        chunk_id = f"{aid}::chunk::{c.chunk_index}"
        content = c.content[:MD_MAX_LEN]
        # truncation is rare but tracked for the first batch only
        result.append(
            {
                "chunk_id": chunk_id,
                "arxiv_id": aid,
                "chunk_idx": c.chunk_index,
                "heading": "",
                "content_text": content,
            }
        )
    return result, None


# ── Embedding pipeline ─────────────────────────────────────────────────────


async def _embed_chunks(texts: list[str]) -> list[list[float]]:
    from compass.pipeline.embedder import Embedder

    async with Embedder() as e:
        return await e.embed_many(texts)


# ── Sparse encoder (per-worker singleton) ──────────────────────────────────

_sparse_encoder = None


def _get_sparse_encoder() -> Any:
    global _sparse_encoder
    if _sparse_encoder is not None:
        return _sparse_encoder
    from compass.pipeline.sparse_encoder import SparseEncoder

    ckpt_path = str(storage.checkpoint_path("bm25", "arxiv.pkl"))
    if not Path(ckpt_path).exists():
        raise FileNotFoundError(f"BM25 checkpoint not found: {ckpt_path}")
    _sparse_encoder = SparseEncoder.load(ckpt_path)
    return _sparse_encoder


# ── Milvus safe ingestion ──────────────────────────────────────────────────


def _safe_insert_chunks_for_paper(client: Any, paper_chunks: list[dict]) -> int:
    """Upsert chunks — store 层安全方法，单次原子操作。"""
    from compass.store.ingest import upsert_arxiv_chunks

    upsert_arxiv_chunks(paper_chunks)
    return len(paper_chunks)


# ── Worker process ──────────────────────────────────────────────────────────


def _worker_setup():
    """使 worker 进程的子进程忽略 SIGINT（Polars/SciPy 子进程）。"""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


# 模块级变量，worker 初始化时填充
_worker_client = None
_worker_cp = None


def _worker_init(ckpt_dir: str):
    """每个 worker 启动时调用一次：加载 BM25、连 Milvus、读 checkpoint."""
    global _worker_client, _worker_cp

    # Ignore SIGINT in worker children
    _worker_setup()

    # 静音 structlog（worker 不写文件日志，但保留 stderr 用于异常追踪）
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(40))

    # Milvus client
    from compass.store.client import get_client

    _worker_client = get_client()

    # BM25 (warm load)
    try:
        _get_sparse_encoder()
    except Exception:
        pass  # will fail fast at first encode

    # Checkpoint (只读，写用 append)
    _worker_cp = Checkpoint(Path(ckpt_dir))


def _process_batch(papers: list[dict]) -> BatchResult:
    """Worker 处理一批论文：chunk → embed → sparse → insert → checkpoint."""
    client = _worker_client
    cp = _worker_cp
    result = BatchResult()

    if client is None or cp is None:
        result.error = "worker not initialized"
        return result

    # ── Step 1: 读 + chunk ────────────────────────────────────────────
    paper_chunks_map: dict[str, list[dict]] = {}
    for p in papers:
        chunks, err = _process_one_paper(p["arxiv_id"], p["created"])
        if err:
            cp.mark_failed(p["arxiv_id"])
            result.fail += 1
            continue
        paper_chunks_map[p["arxiv_id"]] = chunks

    if not paper_chunks_map:
        return result  # all failed

    # ── Step 2: 批量 embed ────────────────────────────────────────────
    all_chunks = [c for chs in paper_chunks_map.values() for c in chs]
    texts = [c["content_text"] for c in all_chunks]

    for emb_start in range(0, len(texts), EMBED_BATCH):
        emb_texts = texts[emb_start : emb_start + EMBED_BATCH]
        emb_chunks = all_chunks[emb_start : emb_start + EMBED_BATCH]
        try:
            embeddings = asyncio.run(_embed_chunks(emb_texts))
        except Exception:
            # 标记这一子批的所有论文失败
            affected = {c["arxiv_id"] for c in emb_chunks}
            for aid in affected:
                cp.mark_failed(aid)
                paper_chunks_map.pop(aid, None)
                result.fail += 1
            continue
        if len(embeddings) != len(emb_texts):
            affected = {c["arxiv_id"] for c in emb_chunks}
            for aid in affected:
                cp.mark_failed(aid)
                paper_chunks_map.pop(aid, None)
                result.fail += 1
            continue
        for i in range(len(emb_chunks)):
            emb_chunks[i]["content_embedding"] = embeddings[i]

    # 重建（可能有一批次的嵌入失败被移除了）
    all_chunks = [c for chs in paper_chunks_map.values() for c in chs]
    if not all_chunks:
        return result
    texts = [c["content_text"] for c in all_chunks]

    # ── Step 3: Sparse encode ─────────────────────────────────────────
    try:
        enc = _get_sparse_encoder()
        sparse_vecs = enc.encode(texts, batch_size=SPARSE_BATCH)
    except Exception:
        for aid in paper_chunks_map:
            cp.mark_failed(aid)
            result.fail += len(paper_chunks_map)
        return result

    if len(sparse_vecs) != len(texts):
        for aid in paper_chunks_map:
            cp.mark_failed(aid)
            result.fail += len(paper_chunks_map)
        return result

    for i in range(len(all_chunks)):
        all_chunks[i]["content_sparse"] = sparse_vecs[i] or {
            0: 0.0
        }  # 空向量兜底，避免 validate 报错

    # ── Step 4: Attach paper_title ────────────────────────────────────
    title_map = {p["arxiv_id"]: p.get("title", "")[:TITLE_MAX_LEN] for p in papers}
    for c in all_chunks:
        c["paper_title"] = title_map.get(c["arxiv_id"], "")

    # ── Step 5: 按论文入库 ─────────────────────────────────────────────
    from compass.store.ingest import update_arxiv_paper

    by_paper: dict[str, list[dict]] = {}
    for c in all_chunks:
        by_paper.setdefault(c["arxiv_id"], []).append(c)

    for aid, pchunks in by_paper.items():
        try:
            n = _safe_insert_chunks_for_paper(client, pchunks)
            update_arxiv_paper(aid, {"has_chunks": True})
            cp.mark_done(aid)
            result.chunks += n
            result.done += 1
        except Exception:
            cp.mark_failed(aid)
            result.fail += 1

    return result


# ── Shutdown flag ────────────────────────────────────────────────────────────

_shutdown_requested = False


def _on_shutdown(signum: int, _frame: object) -> None:
    global _shutdown_requested
    _shutdown_requested = True


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    signal.signal(signal.SIGINT, _on_shutdown)
    signal.signal(signal.SIGTERM, _on_shutdown)

    parser = argparse.ArgumentParser()
    parser.add_argument("--max-rows", type=int, default=0, help="最多处理论文数 (0=全量)")
    parser.add_argument("--num-workers", type=int, default=1, help="Worker 进程数 (1=单进程)")
    parser.add_argument("--delete-ckpt", action="store_true", help="清空 checkpoint + 截断日志")
    parser.add_argument("--log-level", type=str, default="INFO")
    args = parser.parse_args()

    # ── Logging ──────────────────────────────────────────────────────────
    log_path = storage.log_path("ingest_chunks", "ingest_chunks.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_mode = "w" if args.delete_ckpt else "a"
    configure_logging(
        log_level=args.log_level,
        use_json=False,
        file_handler=(str(log_path), LOG_ROTATION_BYTES, LOG_BACKUP_COUNT),
        mode=log_mode,
    )
    log = structlog.get_logger("ingest_chunks")
    log.info(
        "started", max_rows=args.max_rows or "∞", num_workers=args.num_workers, log_mode=log_mode
    )

    # ── Checkpoint ──────────────────────────────────────────────────────
    ckpt_dir = storage.checkpoint_dir("ingest_chunks")
    if args.delete_ckpt:
        for tag in ("done_ids", "failed_ids"):
            f = ckpt_dir / f"{tag}.txt"
            if f.exists():
                f.unlink()
        log.info("checkpoint_deleted")

    cp = Checkpoint(ckpt_dir)
    log.info("checkpoint_loaded", done=len(cp.done), failed=len(cp.failed))

    # ── 单进程模式：主进程初始化 worker 上下文 ───────────────────────────
    if args.num_workers == 1:
        _worker_init(str(ckpt_dir))
        try:
            _get_sparse_encoder()
            log.info("sparse_encoder_loaded")
        except Exception:
            log.exception("sparse_encoder_load_failed")
            sys.exit(1)

    stats = Stats()

    # ── Worker pool ──────────────────────────────────────────────────────
    pool = None
    if args.num_workers > 1:
        # fork 模式：Linux 默认，子进程继承父进程状态，无需 pickle __main__ 函数
        pool = mp.Pool(
            processes=args.num_workers,
            initializer=_worker_init,
            initargs=(str(ckpt_dir),),
        )
        log.info("worker_pool_created", workers=args.num_workers)

    # ── Cursor-scan ──────────────────────────────────────────────────────
    from compass.store.client import QUERY_CONSISTENCY, escape_sql, get_client

    client = get_client()
    log.info("cursor_scan_start")

    total_processed = 0
    last_id = ""
    batch_no = 0

    while not _shutdown_requested:
        if args.max_rows and total_processed >= args.max_rows:
            break

        filt = (
            f"has_markdown == True and has_chunks == False and arxiv_id > '{escape_sql(last_id)}'"
            if last_id
            else "has_markdown == True and has_chunks == False"
        )
        rows = client.query(
            "arxiv_papers",
            filter=filt,
            output_fields=["arxiv_id", "created", "title"],
            consistency_level=QUERY_CONSISTENCY,
            limit=SCAN_BATCH,
        )
        if not rows:
            log.info("cursor_scan_done", total_processed=total_processed)
            break

        last_id = rows[-1]["arxiv_id"]
        stats.scanned += len(rows)

        # Filter + prepare papers
        papers: list[dict] = []
        batch_skip = 0
        for r in rows:
            aid = r["arxiv_id"]
            if aid in cp.done or aid in cp.failed:
                batch_skip += 1
                continue
            created = r.get("created", "") or ""
            if not created:
                continue
            papers.append(
                {
                    "arxiv_id": aid,
                    "created": created,
                    "title": r.get("title", ""),
                }
            )

        stats.skip_done += batch_skip

        log.info(
            "cursor_batch",
            batch=batch_no,
            rows=len(rows),
            papers=len(papers),
            skip=len(rows) - len(papers),
            last_id=last_id,
            total_processed=total_processed,
        )

        if not papers:
            if len(rows) < SCAN_BATCH:
                break
            batch_no += 1
            continue

        # Split into BATCH_SIZE batches
        batches = [papers[i : i + BATCH_SIZE] for i in range(0, len(papers), BATCH_SIZE)]

        # Enforce --max-rows limit on batch count
        if args.max_rows:
            remaining = args.max_rows - total_processed
            if remaining <= 0:
                break
            # Only dispatch enough batches to hit max_rows
            i, taken = 0, 0
            while i < len(batches) and taken < remaining:
                taken += len(batches[i])
                i += 1
            batches = batches[:i]

        # ── Process ───────────────────────────────────────────────────
        if pool and len(batches) > 1:
            # Multi-worker: imap_unordered
            t_batch = time.monotonic()
            jobs = [(b,) for b in batches]  # type: ignore
            results = []
            for r in pool.imap_unordered(_process_batch, batches):
                results.append(r)
                # Progress update every 5 batches
                if len(results) % 5 == 0:
                    done = sum(r.done for r in results)
                    fail = sum(r.fail for r in results)
                    ch = sum(r.chunks for r in results)
                    elapsed = time.monotonic() - t_batch
                    rate = done / elapsed if elapsed > 0 else 0
                    log.info(
                        "worker_progress",
                        batches_done=len(results),
                        batches_total=len(batches),
                        done=done,
                        fail=fail,
                        chunks=ch,
                        elapsed=round(elapsed),
                        rate=f"{rate:.1f}/s",
                    )
                    # ── Fuse: 连续多批 0 done → 立刻熔断 ─────────
                    if len(results) >= 10 and done == 0:
                        log.error("fuse_tripped", batches_done=len(results), fail=fail)
                        break

            elapsed = time.monotonic() - t_batch
            done = sum(r.done for r in results)
            fail = sum(r.fail for r in results)
            ch = sum(r.chunks for r in results)
            rate = done / elapsed if elapsed > 0 else 0

            stats.done += done
            stats.fail += fail
            stats.total_chunks += ch
            total_processed += done + fail

            log.info(
                "scan_batch_done",
                batch=batch_no,
                papers=len(papers),
                done=done,
                fail=fail,
                chunks=ch,
                elapsed=round(elapsed),
                rate=f"{rate:.1f}/s",
                overall=stats.summary(),
            )
        else:
            # Single worker / serial
            results = []
            for batch in batches:
                if _shutdown_requested or (args.max_rows and total_processed >= args.max_rows):
                    break
                r = _process_batch(batch)
                results.append(r)
                stats.done += r.done
                stats.fail += r.fail
                stats.total_chunks += r.chunks
                total_processed += r.done + r.fail

            log.info(
                "scan_batch_done",
                batch=batch_no,
                papers=len(papers),
                done=stats.done,
                fail=stats.fail,
                chunks=stats.total_chunks,
                overall=stats.summary(),
            )

        batch_no += 1
        if len(rows) < SCAN_BATCH:
            break

    # ── Cleanup ────────────────────────────────────────────────────────
    if pool:
        pool.close()
        pool.join()

    log.info("done", stats=stats.summary())
    print(f"\n  ingest_chunks finished")
    print(f"  {stats.summary()}")
    print(f"  ckpt done:   {cp._done_file}  ({len(cp.done):,} entries)")
    print(f"  ckpt failed: {cp._failed_file}  ({len(cp.failed):,} entries)")


if __name__ == "__main__":
    main()
