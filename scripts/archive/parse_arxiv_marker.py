#!/usr/bin/env python3
"""parse_arxiv_marker.py — 大规模 arXiv PDF 解析 (Marker, 4 GPU x 2 进程 = 8 并发)

架构:
  - 8 个 spawn 子进程, 每张 GPU 2 个 (gpu_id = worker_id // GPU_WORKERS)
  - 每个进程加载 Marker 模型一次 (~3GB), 串行处理队列中的 PDF
  - 主进程 1-for-1 循环: 每收到一个结果, 立即补一个 PDF

输出: parquet (content_list + markdown + figure_images 占位)
断点续传: done_node*.txt + manifest_node*.jsonl
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import queue
import shutil
import sys
import tarfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import structlog

# ── CLI ──────────────────────────────────────────────────────────────────────


def _parse_args():
    _parser = argparse.ArgumentParser(description="arXiv PDF parser (Marker, 32 workers)")
    _parser.add_argument("--max-tars", type=int, default=0)
    _parser.add_argument("--max-papers", type=int, default=0)
    return _parser.parse_args()


# ── Path ─────────────────────────────────────────────────────────────────────

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scholight.logging import configure_logging  # noqa: E402
from scholight.utils import parse_arxiv_id  # noqa: E402

# ── Config ───────────────────────────────────────────────────────────────────

ARXIV_BULK_DIR = Path(os.environ["ARXIV_BULK_DIR"])
DATA_ROOT = os.environ.get(
    "SCHOLIGHT_DATA_ROOT",
    "/inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/academic-data",
)
OUTPUT_DIR = Path(os.environ.get("SCHOLIGHT_MARKER_OUTPUT_DIR", f"{DATA_ROOT}/parsed"))
PET_NODE_RANK = int(os.environ.get("PET_NODE_RANK", "0"))
PET_NNODES = int(os.environ.get("PET_NNODES", "1"))
GPU_WORKERS = int(os.environ.get("SCHOLIGHT_GPU_WORKERS", "2"))
BATCH_FLUSH = int(os.environ.get("SCHOLIGHT_BATCH_FLUSH", "1000"))
HEARTBEAT_SEC = int(os.environ.get("SCHOLIGHT_HEARTBEAT_SEC", "60"))
TMP_DIR = Path(os.environ.get("SCHOLIGHT_TMP_DIR", str(OUTPUT_DIR / "tmp")))

# ── Logger (module-level, configured in main()) ──────────────────────────────

log = structlog.get_logger("parse_arxiv_marker")


# ── GPU detection ────────────────────────────────────────────────────────────


def _detect_gpus() -> int:
    import torch

    if not torch.cuda.is_available():
        log.critical("No CUDA GPU — Marker requires GPU")
        sys.exit(1)
    n = torch.cuda.device_count()
    for i in range(n):
        p = torch.cuda.get_device_properties(i)
        log.info("GPU[%d] %s %.0fGB", i, p.name, p.total_memory / 1e9)
    return n


# ── arXiv ID ─────────────────────────────────────────────────────────────────


def canonical_arxiv_id(member_name: str) -> str:
    fname = Path(member_name).name
    stem = fname.removesuffix(".pdf")
    if len(stem) >= 2 and stem[-2] == "v" and stem[-1].isdigit():
        stem = stem[:-2]
    return stem


# ── Checkpoint ───────────────────────────────────────────────────────────────

CHECKPOINT_DIR = OUTPUT_DIR / "checkpoints"
_file_done = CHECKPOINT_DIR / f"done_node{PET_NODE_RANK}.txt"
_file_manifest = CHECKPOINT_DIR / f"manifest_node{PET_NODE_RANK}.jsonl"
_file_heartbeat = CHECKPOINT_DIR / f"heartbeat_node{PET_NODE_RANK}.txt"
_corrupt_dir = CHECKPOINT_DIR / "corrupt_tars"

_done_buf: list[str] = []
_manifest_buf: list[str] = []
_buf_lock = threading.Lock()
_last_hb = 0.0


def load_corrupt() -> set[str]:
    _corrupt_dir.mkdir(parents=True, exist_ok=True)
    return {p.stem for p in _corrupt_dir.iterdir() if p.is_file()}


def mark_corrupt(tar_path: Path) -> None:
    _corrupt_dir.mkdir(parents=True, exist_ok=True)
    (_corrupt_dir / tar_path.name).touch()


def load_done_ids() -> set[str]:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    ids: set[str] = set()
    for fpath in sorted(CHECKPOINT_DIR.glob("done_node*.txt")):
        try:
            ids.update(line.strip() for line in fpath.read_text().splitlines() if line.strip())
        except Exception as exc:
            log.warning("Checkpoint %s corrupted: %s", fpath.name, exc)
    log.info("Loaded %d completed IDs from checkpoints", len(ids))
    return ids


def mark_done(arxiv_id: str) -> None:
    with _buf_lock:
        _done_buf.append(arxiv_id)
        if len(_done_buf) >= BATCH_FLUSH:
            _flush_buf()


def append_manifest(arxiv_id: str, status: str, pages: int, elapsed: float) -> None:
    record = json.dumps(
        {
            "id": arxiv_id,
            "status": status,
            "pages": pages,
            "elapsed": round(elapsed, 3),
            "node": PET_NODE_RANK,
            "ts": datetime.now(UTC).isoformat(),
        },
        ensure_ascii=False,
    )
    with _buf_lock:
        _manifest_buf.append(record)
        if len(_manifest_buf) >= BATCH_FLUSH:
            _flush_buf()


def _flush_buf() -> None:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    if _done_buf:
        with open(_file_done, "a") as f:
            f.writelines(f"{aid}\n" for aid in _done_buf)
        _done_buf.clear()
    if _manifest_buf:
        with open(_file_manifest, "a") as f:
            for rec in _manifest_buf:
                f.write(rec + "\n")
        _manifest_buf.clear()


def flush_checkpoint() -> None:
    with _buf_lock:
        _flush_buf()


def _heartbeat() -> None:
    global _last_hb
    now = time.monotonic()
    if now - _last_hb < HEARTBEAT_SEC:
        return
    _last_hb = now
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    with open(_file_heartbeat, "w") as f:
        f.write(f"{datetime.now(UTC).isoformat()}\n")


# ── Tar I/O ──────────────────────────────────────────────────────────────────


def list_tars(corrupt: set[str]) -> list[Path]:
    all_tars = sorted(ARXIV_BULK_DIR.glob("*.tar"))
    clean = [t for t in all_tars if t.name not in corrupt]
    if len(clean) < len(all_tars):
        log.info("Skipping %d corrupt tars", len(all_tars) - len(clean))
    return clean


def scan_tar(tar_path: Path) -> list[tuple[str, str]] | None:
    try:
        with tarfile.open(tar_path, "r") as tf:
            return [
                (m.name, canonical_arxiv_id(m.name))
                for m in tf.getmembers()
                if m.name.endswith(".pdf")
            ]
    except Exception as exc:
        log.warning("Corrupt tar %s: %s", tar_path.name, exc)
        return None


def extract_pdf(tar_path: Path, member_name: str) -> bytes | None:
    try:
        with tarfile.open(tar_path, "r") as tf:
            f = tf.extractfile(member_name)
            return f.read() if f else None
    except Exception as exc:
        log.warning("Extract %s from %s failed: %s", member_name, tar_path.name, exc)
        return None


# ── Parquet ──────────────────────────────────────────────────────────────────


def save_parquet(arxiv_id: str, content_list: list[dict[str, Any]], markdown: str) -> Path:
    year, month = parse_arxiv_id(arxiv_id)
    out_dir = OUTPUT_DIR / str(year) / f"{month:02d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{arxiv_id}.parquet"
    table = pa.table(
        {
            "content_list": [
                json.dumps(content_list, ensure_ascii=False) if content_list else "[]"
            ],
            "markdown": [markdown],
            "figure_images": ["[]"],
        }
    )
    pq.write_table(table, path, compression="zstd")
    return path


# ── GPU subprocess (32 spawn workers) ────────────────────────────────────────


def _gpu_worker(in_q: multiprocessing.Queue, out_q: multiprocessing.Queue, worker_id: int) -> None:
    """Spawn subprocess: pin to GPU, load Marker, process tasks until None sentinel."""
    # CRITICAL: set CUDA_VISIBLE_DEVICES BEFORE import torch — CUDA is eagerly
    # initialized on first torch import in spawn children.
    gpu_id = worker_id // GPU_WORKERS  # which physical GPU (0-3)
    os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    import torch

    _the_log = structlog.get_logger("parse_arxiv_marker")

    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict

    gpu_name = torch.cuda.get_device_name(0)
    _the_log.info("worker-%02d GPU[%d] %s — loading Marker models", worker_id, gpu_id, gpu_name)
    converter = PdfConverter(artifact_dict=create_model_dict())
    _the_log.info("worker-%02d GPU[%d] ready", worker_id, gpu_id)

    while True:
        try:
            task = in_q.get(timeout=10)
        except queue.Empty:
            continue

        if task is None:
            _the_log.info("worker-%02d shutting down", worker_id)
            break

        pdf_path, arxiv_id = task
        try:
            _parse_pdf(converter, arxiv_id, pdf_path, out_q)
        except Exception as exc:
            _the_log.warning("worker-%02d %s crashed: %s", worker_id, arxiv_id, exc)
            out_q.put({"arxiv_id": arxiv_id, "error": str(exc), "pages": 0, "elapsed": 0})
        finally:
            Path(pdf_path).unlink(missing_ok=True)


def _parse_pdf(
    converter: Any,
    arxiv_id: str,
    pdf_path: str,
    out_q: multiprocessing.Queue,
) -> None:
    """Parse one PDF — called from GPU worker subprocess."""
    from marker.output import text_from_rendered

    from scholight.utils.marker import marker_block_to_content

    t0 = time.monotonic()
    document = converter.build_document(pdf_path)
    renderer = converter.resolve_dependencies(converter.renderer)
    rendered = renderer(document)
    md_text, _, _imgs = text_from_rendered(rendered)

    cl = []
    for pi, page in enumerate(document.pages):
        for child in page.current_children:
            item = marker_block_to_content(child, document, pi)
            if item:
                cl.append(item)

    out_q.put(
        {
            "arxiv_id": arxiv_id,
            "content_list": cl,
            "markdown": md_text,
            "pages": len(document.pages),
            "elapsed": time.monotonic() - t0,
        }
    )


# ── Main pipeline ────────────────────────────────────────────────────────────


def main() -> None:
    cls = _parse_args()

    # ── Logging ────────────────────────────────────────────────────────────
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR = OUTPUT_DIR / "logs"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"marker_parse_node{PET_NODE_RANK}.log"
    if log_path.exists():
        log_path.rename(
            LOG_DIR / f"marker_parse_node{PET_NODE_RANK}_"
            f"{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.log"
        )
    configure_logging(
        log_level=os.environ.get("SCHOLIGHT_LOG_LEVEL", "INFO"),
        use_json=os.environ.get("SCHOLIGHT_LOG_JSON") == "1" or not sys.stderr.isatty(),
        file_handler=(str(log_path), 100_000_000, 10),
    )
    global log
    log = structlog.get_logger("parse_arxiv_marker")

    # ── Init ───────────────────────────────────────────────────────────────
    t_start = time.monotonic()
    num_gpus = _detect_gpus()
    total_workers = num_gpus * GPU_WORKERS

    log.info(
        "%d GPU x %d workers = %d concurrent | streaming", num_gpus, GPU_WORKERS, total_workers
    )
    log.info("Output: %s", OUTPUT_DIR)

    # ── State ──────────────────────────────────────────────────────────────
    done_ids = load_done_ids()
    init_done = len(done_ids)
    corrupt_cache = load_corrupt()

    all_tars = list_tars(corrupt_cache)
    my_tars = [t for i, t in enumerate(all_tars) if i % PET_NNODES == PET_NODE_RANK]
    if cls.max_tars > 0:
        my_tars = my_tars[: cls.max_tars]
    log.info(
        "Node %d/%d — %d tars of %d total", PET_NODE_RANK, PET_NNODES, len(my_tars), len(all_tars)
    )

    # ── Lazy stream ────────────────────────────────────────────────────────
    def _stream():
        for ti, tar_path in enumerate(my_tars):
            if tar_path.name in corrupt_cache:
                continue
            members = scan_tar(tar_path)
            if members is None:
                mark_corrupt(tar_path)
                corrupt_cache.add(tar_path.name)
                continue
            for mname, aid in members:
                if aid not in done_ids:
                    yield (aid, tar_path, mname)
            if ti % 100 == 0 and ti > 0:
                log.info("Scanned %d/%d tars", ti + 1, len(my_tars))

    stream = _stream()
    stream_exhausted = False

    # ── Spawn 32 workers ───────────────────────────────────────────────────
    mp_ctx = multiprocessing.get_context("spawn")
    out_q = mp_ctx.Queue()
    in_queues = [mp_ctx.Queue() for _ in range(total_workers)]
    procs = []
    for wid in range(total_workers):
        p = mp_ctx.Process(target=_gpu_worker, args=(in_queues[wid], out_q, wid), daemon=False)
        p.start()
        procs.append(p)
    log.info("Launched %d GPU workers", total_workers)

    stats = {"ok": 0, "empty": 0, "extract_fail": 0, "parse_fail": 0, "save_fail": 0}
    total_new = 0
    in_flight = 0  # number of PDFs currently being processed
    feed_queue = 0

    # ── Priming: fill each queue with 1 PDF ────────────────────────────────
    for wid in range(total_workers):
        try:
            aid, tp, mn = next(stream)
        except StopIteration:
            stream_exhausted = True
            break
        _heartbeat()
        pdf_bytes = extract_pdf(tp, mn)
        if pdf_bytes is None:
            stats["extract_fail"] += 1
            append_manifest(aid, "extract_failed", 0, 0)
            continue
        tmp = TMP_DIR / f"{aid}.pdf"
        tmp.write_bytes(pdf_bytes)
        in_queues[wid].put((str(tmp), aid))
        in_flight += 1

    log.info("Primed %d/%d workers", in_flight, total_workers)

    # ── Main loop: 1-out, 1-in ─────────────────────────────────────────────
    _stall_count = 0
    while in_flight > 0:
        try:
            r = out_q.get(timeout=600)  # 10min per result; timeout = worker died
        except queue.Empty:
            _stall_count += 1
            log.warning("No result for 10min — %d in-flight, may be dead", in_flight)
            if _stall_count >= 3:
                log.error("30min no result — all workers likely dead, aborting")
                break
            continue

        _stall_count = 0
        in_flight -= 1

        aid = r.get("arxiv_id", "?")
        if r.get("error"):
            stats["parse_fail"] += 1
            append_manifest(aid, "parse_failed", r.get("pages", 0), r.get("elapsed", 0))
        elif r.get("content_list"):
            try:
                save_parquet(aid, r["content_list"], r.get("markdown", ""))
                stats["ok"] += 1
                total_new += 1
            except Exception as exc:
                log.error("%s save failed: %s", aid, exc)
                stats["save_fail"] += 1
                append_manifest(aid, "save_failed", r.get("pages", 0), r.get("elapsed", 0))
            mark_done(aid)
            done_ids.add(aid)
            append_manifest(aid, "ok", r.get("pages", 0), r.get("elapsed", 0))
        else:
            stats["empty"] += 1
            mark_done(aid)
            done_ids.add(aid)
            append_manifest(aid, "empty", r.get("pages", 0), r.get("elapsed", 0))

        # ── Feed next PDF to the queue that just freed up ───────────────
        if not stream_exhausted:
            try:
                aid, tp, mn = next(stream)
            except StopIteration:
                stream_exhausted = True
                log.info("Stream exhausted — %d in-flight remaining", in_flight)
                continue
            _heartbeat()
            pdf_bytes = extract_pdf(tp, mn)
            if pdf_bytes is None:
                stats["extract_fail"] += 1
                append_manifest(aid, "extract_failed", 0, 0)
                continue
            tmp = TMP_DIR / f"{aid}.pdf"
            tmp.write_bytes(pdf_bytes)
            # Round-robin: feed to next slot so all workers stay busy
            in_queues[feed_queue % total_workers].put((str(tmp), aid))
            feed_queue = (feed_queue + 1) % total_workers
            in_flight += 1

        if total_new % 50 == 0 and total_new > 0:
            elapsed = time.monotonic() - t_start
            log.info("Progress: %d papers (%.1f/min)", total_new, total_new / max(elapsed, 1) * 60)

        if cls.max_papers > 0 and total_new >= cls.max_papers:
            log.info("Max papers (%d) reached", cls.max_papers)
            break

    # ── Shutdown ────────────────────────────────────────────────────────────
    import contextlib

    for in_q in in_queues:
        with contextlib.suppress(Exception):
            in_q.put(None, timeout=2)
    for p in procs:
        p.join(timeout=30)
        if p.is_alive():
            p.terminate()

    # ── Summary ─────────────────────────────────────────────────────────────
    flush_checkpoint()
    elapsed = time.monotonic() - t_start
    log.info("=" * 50)
    log.info(
        "DONE  elapsed: %.1f min  rate: %.0f papers/min  new: %d  total: %d",
        elapsed / 60,
        total_new / max(elapsed, 1) * 60,
        total_new,
        init_done + total_new,
    )
    log.info(
        "      ok=%d empty=%d extract_fail=%d parse_fail=%d save_fail=%d",
        stats["ok"],
        stats["empty"],
        stats["extract_fail"],
        stats["parse_fail"],
        stats["save_fail"],
    )
    shutil.rmtree(TMP_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
