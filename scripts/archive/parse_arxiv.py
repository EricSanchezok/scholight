#!/usr/bin/env python3
"""
parse_arxiv.py — 大规模 arXiv PDF 解析 (MinerU2.5-Pro + vLLM async engine)

用法:
  python scripts/parse_arxiv.py                           # 全量解析
  python scripts/parse_arxiv.py --max-tars 3              # 测试: 只处理3个tar
  python scripts/parse_arxiv.py --max-papers 20           # 测试: 解析20篇即停

环境变量:
  PET_NNODES              总节点数 (启智平台注入)
  PET_NODE_RANK           当前节点 rank (启智平台注入)
  MINERU_MODEL_PATH       模型路径 (必需)
  ARXIV_BULK_DIR          tar 目录 (只读)
  OUTPUT_DIR              parquet + checkpoint 输出目录

可选:
  SCHOLIGHT_LOG_LEVEL       日志级别 (默认 INFO)
  SCHOLIGHT_BATCH_FLUSH     checkpoint 批量落盘条数 (默认 1000)
  SCHOLIGHT_HEARTBEAT_SEC   心跳间隔秒数 (默认 60)
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import os
import sys
import tarfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import structlog

# ── CLI ──────────────────────────────────────────────────────────────────────

_parser = argparse.ArgumentParser(description="arXiv PDF parser (MinerU2.5-Pro + vLLM)")
_parser.add_argument("--max-tars", type=int, default=0, help="Limit tars to process (0=all)")
_parser.add_argument("--max-papers", type=int, default=0, help="Limit papers to process (0=all)")
_args = _parser.parse_args()

# ── Logging ─────────────────────────────────────────────────────────────────

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scholight.logging import configure_logging  # noqa: E402
from scholight.utils import parse_arxiv_id  # noqa: E402

configure_logging(
    log_level=os.environ.get("SCHOLIGHT_LOG_LEVEL", "INFO"),
    use_json=os.environ.get("SCHOLIGHT_LOG_JSON") == "1" or not sys.stderr.isatty(),
    file_handler=None,
)
log = structlog.get_logger("parse_arxiv")

# ── Config ───────────────────────────────────────────────────────────────────

MODEL_PATH = Path(os.environ["MINERU_MODEL_PATH"])
ARXIV_BULK_DIR = Path(os.environ["ARXIV_BULK_DIR"])
_data_root = os.environ.get(
    "SCHOLIGHT_DATA_ROOT", "/inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/academic-data"
)
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", f"{_data_root}/parsed"))

PET_NODE_RANK = int(os.environ.get("PET_NODE_RANK", "0"))
PET_NNODES = int(os.environ.get("PET_NNODES", "1"))

PAGE_BATCH_SIZE = int(os.environ.get("SCHOLIGHT_PAGE_BATCH_SIZE", "64"))
PAPER_BATCH_SIZE = int(os.environ.get("SCHOLIGHT_PAPER_BATCH_SIZE", "8"))
RENDER_WORKERS = int(os.environ.get("SCHOLIGHT_RENDER_WORKERS", "8"))
VLLM_PORT = int(os.environ.get("VLLM_PORT", "8000"))
MAX_MODEL_LEN = int(os.environ.get("SCHOLIGHT_MAX_MODEL_LEN", "8192"))
MAX_CONCURRENCY = int(os.environ.get("SCHOLIGHT_MAX_CONCURRENCY", "100"))
BATCH_FLUSH = int(os.environ.get("SCHOLIGHT_BATCH_FLUSH", "1000"))
HEARTBEAT_SEC = int(os.environ.get("SCHOLIGHT_HEARTBEAT_SEC", "60"))

# ── GPU detection (lazy — CUDA init must be deferred for vLLM V1 multiproc) ──


def _detect_gpus() -> int:
    import torch

    if not torch.cuda.is_available():
        return 0
    return torch.cuda.device_count()


NUM_GPUS: int = 0  # resolved in main()


def _resolve_gpus() -> int:
    global NUM_GPUS
    NUM_GPUS = _detect_gpus()
    if NUM_GPUS == 0:
        log.critical("No GPU available — vLLM requires CUDA. Aborting.")
        sys.exit(1)
    return NUM_GPUS


# ── Checkpoint system ────────────────────────────────────────────────────────

CHECKPOINT_DIR = OUTPUT_DIR / "checkpoints"
MY_DONE_IDS_FILE = CHECKPOINT_DIR / f"done_ids_node{PET_NODE_RANK}.txt"
MY_MANIFEST_FILE = CHECKPOINT_DIR / f"manifest_node{PET_NODE_RANK}.jsonl"
HEARTBEAT_FILE = CHECKPOINT_DIR / f"heartbeat_node{PET_NODE_RANK}.txt"

_done_buffer: list[str] = []
_manifest_buffer: list[str] = []
_buf_lock = threading.Lock()

# Corrupt tar markers — written once, skipped on all subsequent runs
_corrupt_tar_dir = CHECKPOINT_DIR / "corrupt_tars"


def load_corrupt_tars() -> set[str]:
    _corrupt_tar_dir.mkdir(parents=True, exist_ok=True)
    return {p.stem for p in _corrupt_tar_dir.iterdir() if p.is_file()}


def mark_tar_corrupt(tar_path: Path) -> None:
    _corrupt_tar_dir.mkdir(parents=True, exist_ok=True)
    (_corrupt_tar_dir / tar_path.name).touch()


# ── Checkpoint I/O ───────────────────────────────────────────────────────────


def load_done_ids() -> set[str]:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    ids: set[str] = set()
    for fpath in sorted(CHECKPOINT_DIR.glob("done_ids_node*.txt")):
        try:
            with open(fpath) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        ids.add(line)
        except Exception:
            log.warning("Failed to read checkpoint %s", fpath.name)
    log.info("Loaded %s completed arxiv_ids from checkpoints", len(ids))
    return ids


def mark_done(arxiv_id: str) -> None:
    """记入 buffer，累计 BATCH_FLUSH 条后一次写盘."""
    with _buf_lock:
        _done_buffer.append(arxiv_id)
        if len(_done_buffer) >= BATCH_FLUSH:
            _flush_buffers()


def append_manifest(arxiv_id: str, status: str, pages: int, elapsed: float) -> None:
    record = json.dumps(
        {
            "arxiv_id": arxiv_id,
            "status": status,
            "pages": pages,
            "elapsed_sec": round(elapsed, 2),
            "node_rank": PET_NODE_RANK,
            "timestamp": datetime.now(UTC).isoformat(),
        },
        ensure_ascii=False,
    )
    with _buf_lock:
        _manifest_buffer.append(record)
        if len(_manifest_buffer) >= BATCH_FLUSH:
            _flush_buffers()


def _flush_buffers() -> None:
    """批量落盘 done_ids + manifest（调用方已持有 _buf_lock）."""
    if _done_buffer:
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        with open(MY_DONE_IDS_FILE, "a") as f:
            f.writelines(f"{aid}\n" for aid in _done_buffer)
        _done_buffer.clear()
    if _manifest_buffer:
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        with open(MY_MANIFEST_FILE, "a") as f:
            for rec in _manifest_buffer:
                f.write(rec)
                f.write("\n")
        _manifest_buffer.clear()


def flush_checkpoint() -> None:
    """退出前强制落盘残余 buffer."""
    with _buf_lock:
        _flush_buffers()


# ── Liveness heartbeat ───────────────────────────────────────────────────────

_last_heartbeat = 0.0


def _touch_heartbeat() -> None:
    global _last_heartbeat
    now = time.monotonic()
    if now - _last_heartbeat < HEARTBEAT_SEC:
        return
    _last_heartbeat = now
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_FILE.write_text(f"{datetime.now(UTC).isoformat()}\n")


# ── arXiv ID canonicalisation ────────────────────────────────────────────────


def canonical_arxiv_id(member_name: str) -> str:
    """将 tar 成员路径规范化为标准 arXiv ID.

    "1901/1901.00293v2.pdf" → "1901.00293"
    "hep-th/9901001.pdf"    → "9901.00001" (从 hep-th 年份+编号推导)
    """
    # 去路径前缀，只留文件名
    fname = Path(member_name).name
    # 去 .pdf 后缀和版本号 (v1, v2, v3)
    stem = fname.removesuffix(".pdf")
    if stem[-2] == "v" and stem[-1].isdigit():
        stem = stem[:-2]

    # 新式 ID: "1901.00293"
    if "." in stem:
        return stem

    # 旧式 ID: "9901001" (hep-th/cond-mat 等去掉分类前缀后)
    # 所有旧式 ID 都是 YYMM + 序号，从 9107 开始
    # 我们保持原始格式作为 arxiv_id
    return stem


# ── PDF → Images ─────────────────────────────────────────────────────────────


# ── Tar I/O ──────────────────────────────────────────────────────────────────


def list_tars(corrupt_tars: set[str]) -> list[Path]:
    all_tars = sorted(ARXIV_BULK_DIR.glob("*.tar"))
    clean = [t for t in all_tars if t.name not in corrupt_tars]
    skipped = len(all_tars) - len(clean)
    if skipped:
        log.info("Skipping %d corrupt tar(s) from previous runs", skipped)
    log.info("Found %d tar files (%d clean)", len(clean), len(all_tars))
    return clean


def scan_tar_members(tar_path: Path) -> list[tuple[str, str]] | None:
    """返回 (member_path, canonical_arxiv_id) 列表; 损坏返回 None."""
    try:
        with tarfile.open(tar_path, "r") as tar:
            members: list[tuple[str, str]] = []
            for m in tar.getmembers():
                if m.name.endswith(".pdf"):
                    aid = canonical_arxiv_id(m.name)
                    members.append((m.name, aid))
            return members
    except Exception as e:
        log.warning("Corrupt tar %s: %s", tar_path.name, e)
        return None


def extract_pdf(tar_path: Path, member_name: str) -> bytes | None:
    try:
        with tarfile.open(tar_path, "r") as tar:
            f = tar.extractfile(member_name)
            if f:
                return f.read()
    except Exception as e:
        log.warning("Extract %s from %s: %s", member_name, tar_path.name, e)
    return None


# ── Parquet output ───────────────────────────────────────────────────────────


def save_parquet(arxiv_id: str, content_list: list) -> Path:
    year, month = parse_arxiv_id(arxiv_id)
    out_dir = OUTPUT_DIR / str(year) / f"{month:02d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{arxiv_id}.parquet"
    table = pa.table(
        {
            "content_list": [
                json.dumps(content_list, ensure_ascii=False) if content_list else "[]"
            ],
            "figure_images": ["[]"],
        }
    )
    pq.write_table(table, path, compression="zstd")
    return path


# ── vLLM Server + MinerUClient http-client (works in Docker, no multiproc issues) ──

import subprocess as _sp

_vllm_proc: subprocess.Popen[str] | None = None  # type: ignore[name-defined]
_client: MinerUClient | None = None  # type: ignore[name-defined]


def start_vllm_server() -> None:
    """启动 vLLM server — TP=1 单卡, max batch 拉满."""
    global _vllm_proc
    log.info("Starting vLLM server (tp=1, port=%d)...", VLLM_PORT)

    cmd = [
        sys.executable,
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        str(MODEL_PATH),
        "--tensor-parallel-size",
        "1",
        "--gpu-memory-utilization",
        "0.95",
        "--max-model-len",
        str(MAX_MODEL_LEN),
        "--max-num-batched-tokens",
        "32768",
        "--port",
        str(VLLM_PORT),
        "--host",
        "0.0.0.0",
        "--logits-processors",
        "mineru_vl_utils:MinerULogitsProcessor",
        "--disable-log-requests",
    ]
    _vllm_proc = _sp.Popen(
        cmd,
        stdout=_sp.PIPE,
        stderr=_sp.STDOUT,
        text=True,
        bufsize=1,
    )

    def _pipe_output() -> None:
        assert _vllm_proc and _vllm_proc.stdout
        for line in _vllm_proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            if "ERROR" in line or "WARNING" in line:
                log.error("[vLLM] %s", line)
            elif any(kw in line for kw in ("throughput", "Running:", "KV cache")):
                continue  # suppress per-10s stats noise
            else:
                log.info("[vLLM] %s", line)

    threading.Thread(target=_pipe_output, daemon=True).start()


async def wait_vllm_ready(timeout: int = 600) -> bool:
    """轮询 /health 直到 vLLM 就绪."""
    import httpx

    url = f"http://localhost:{VLLM_PORT}/health"
    start = time.monotonic()
    async with httpx.AsyncClient(timeout=5) as client:
        while time.monotonic() - start < timeout:
            if _vllm_proc and _vllm_proc.poll() is not None:
                log.error("vLLM process exited during startup")
                return False
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    log.info("vLLM ready (%.0fs)", time.monotonic() - start)
                    return True
            except Exception:
                pass
            await asyncio.sleep(5)
    log.error("vLLM did not become ready within %ds", timeout)
    return False


def stop_vllm_server() -> None:
    if _vllm_proc and _vllm_proc.poll() is None:
        log.info("Stopping vLLM server...")
        _vllm_proc.terminate()
        try:
            _vllm_proc.wait(timeout=60)
        except Exception:
            _vllm_proc.kill()
        log.info("vLLM server stopped")


def init_mineru_client() -> None:
    """初始化 MinerUClient — http-client, max_concurrency 拉满."""
    global _client
    from mineru_vl_utils import MinerUClient

    start_vllm_server()
    if not wait_vllm_ready_sync():
        log.critical("vLLM server failed to start")
        sys.exit(1)

    _client = MinerUClient(
        backend="http-client",
        server_url=f"http://localhost:{VLLM_PORT}",
        max_concurrency=MAX_CONCURRENCY,
        use_tqdm=False,
    )
    log.info("MinerUClient ready (max_concurrency=%d)", MAX_CONCURRENCY)


def wait_vllm_ready_sync(timeout: int = 600) -> bool:
    """同步轮询 /health (供 init 阶段使用)."""
    import httpx

    url = f"http://localhost:{VLLM_PORT}/health"
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if _vllm_proc and _vllm_proc.poll() is not None:
            log.error("vLLM process exited during startup")
            return False
        try:
            resp = httpx.get(url, timeout=5)
            if resp.status_code == 200:
                log.info("vLLM ready (%.0fs)", time.monotonic() - start)
                return True
        except Exception:
            pass
        time.sleep(5)
    log.error("vLLM did not become ready within %ds", timeout)
    return False


def pdf_page_count(pdf_bytes: bytes) -> int:
    """获取 PDF 页数（用 pdf2image 的 pdfinfo, 和 convert_from_bytes 同一后端）."""
    from pdf2image import pdfinfo_from_bytes

    try:
        info = pdfinfo_from_bytes(pdf_bytes)
        return info["Pages"]
    except Exception:
        return 0


def render_paper(pdf_bytes: bytes) -> list:
    """渲染论文所有页面为 PIL Image 列表 (多线程 pdftoppm)."""
    from pdf2image import convert_from_bytes

    total = pdf_page_count(pdf_bytes)
    if total == 0:
        return []
    return convert_from_bytes(pdf_bytes, dpi=144, thread_count=4)


def parse_batch_papers(papers: list[tuple[str, bytes]]) -> list[tuple[str, list]]:
    """批量解析多篇论文 — 多线程渲染 + 混合投递 VLM.

    papers: [(arxiv_id, pdf_bytes), ...]
    returns: [(arxiv_id, content_list), ...]
    """
    from concurrent.futures import ThreadPoolExecutor

    assert _client is not None

    # ── 1. 多线程并行渲染所有论文的所有页 ──────────────────
    with ThreadPoolExecutor(max_workers=RENDER_WORKERS) as pool:
        render_results = list(
            pool.map(
                lambda item: (item[0], render_paper(item[1])),
                papers,
            )
        )

    # ── 2. 收集所有页面 + 记录归属 ────────────────────────
    all_images: list = []
    paper_ranges: list[tuple[str, int, int]] = []  # (arxiv_id, start, end)

    for arxiv_id, pages in render_results:
        if not pages:
            paper_ranges.append((arxiv_id, -1, -1))  # empty marker
            continue
        start = len(all_images)
        all_images.extend(pages)
        end = len(all_images)
        paper_ranges.append((arxiv_id, start, end))

    if not all_images:
        return [(aid, []) for aid, _, _ in paper_ranges]

    # ── 3. 分包混合投递: 每 ~200 页一个 batch, 连续轰炸 vLLM ───
    CHUNK = 200
    all_results: list = []
    for chunk_start in range(0, len(all_images), CHUNK):
        chunk = all_images[chunk_start : chunk_start + CHUNK]
        all_results.extend(_client.batch_two_step_extract(chunk))
    log.info("VLM done: %d pages from %d papers", len(all_images), len(papers))

    # ── 4. 按归属拆分结果 ─────────────────────────────────
    output: list[tuple[str, list]] = []
    for arxiv_id, start, end in paper_ranges:
        if start == -1:
            output.append((arxiv_id, []))
        else:
            output.append((arxiv_id, all_results[start:end]))

    return output


# ── Main ─────────────────────────────────────────────────────────────────────


async def main_async() -> None:
    t_start = time.monotonic()

    # ── Load state ──────────────────────────────────────────────────────────
    done_ids = load_done_ids()
    corrupt_tars = load_corrupt_tars()

    # ── Tar inventory ───────────────────────────────────────────────────────
    all_tars = list_tars(corrupt_tars)
    my_tars = [t for i, t in enumerate(all_tars) if i % PET_NNODES == PET_NODE_RANK]
    if _args.max_tars > 0:
        my_tars = my_tars[: _args.max_tars]
    log.info("Node %s: %s tars assigned (of %s total)", PET_NODE_RANK, len(my_tars), len(all_tars))

    # ── Init MinerU ─────────────────────────────────────────────────────────
    await asyncio.to_thread(init_mineru_client)

    # ── Process ─────────────────────────────────────────────────────────────
    total_done = len(done_ids)
    total_new = 0
    stats: dict[str, int] = {
        "ok": 0,
        "empty": 0,
        "extract_failed": 0,
        "parse_failed": 0,
        "save_failed": 0,
    }

    for i, tar_path in enumerate(my_tars):
        _touch_heartbeat()

        members = scan_tar_members(tar_path)
        if members is None:
            mark_tar_corrupt(tar_path)
            continue

        pending = [(n, a) for n, a in members if a not in done_ids]

        if not pending:
            continue

        pct = (i + 1) / len(my_tars) * 100
        log.info(
            "[%s] %d pending / %d total | tar %d/%d (%.1f%%) | done: %d +%d",
            tar_path.name,
            len(pending),
            len(members),
            i + 1,
            len(my_tars),
            pct,
            total_done,
            total_new,
        )

        for batch_start in range(0, len(pending), PAPER_BATCH_SIZE):
            if _args.max_papers > 0 and total_new >= _args.max_papers:
                log.info("Max papers limit (%d) reached — stopping", _args.max_papers)
                break

            batch = pending[batch_start : batch_start + PAPER_BATCH_SIZE]
            _touch_heartbeat()

            # ── Extract PDFs ──────────────────────────────────────
            paper_inputs: list[tuple[str, bytes]] = []
            for member_name, arxiv_id in batch:
                pdf_bytes = extract_pdf(tar_path, member_name)
                if not pdf_bytes:
                    stats["extract_failed"] += 1
                    append_manifest(arxiv_id, "extract_failed", 0, 0)
                    continue
                paper_inputs.append((arxiv_id, pdf_bytes))

            if not paper_inputs:
                continue

            # ── Batch parse (render + VLM) ────────────────────────
            t0 = time.monotonic()
            try:
                results = await asyncio.to_thread(parse_batch_papers, paper_inputs)
            except Exception as e:
                log.error("Batch parse failed: %s", e)
                for arxiv_id, _ in paper_inputs:
                    stats["parse_failed"] += 1
                    append_manifest(arxiv_id, f"parse_failed:{e}", 0, time.monotonic() - t0)
                continue

            batch_elapsed = time.monotonic() - t0

            # ── Save results ──────────────────────────────────────
            for arxiv_id, content_list in results:
                elapsed_per = batch_elapsed / max(len(results), 1)

                if not content_list:
                    stats["empty"] += 1
                    mark_done(arxiv_id)
                    append_manifest(arxiv_id, "empty", 0, elapsed_per)
                    continue

                try:
                    save_parquet(arxiv_id, content_list)
                except Exception as e:
                    log.error("%s save failed: %s", arxiv_id, e)
                    stats["save_failed"] += 1
                    append_manifest(arxiv_id, f"save_failed:{e}", 0, elapsed_per)
                    continue

                mark_done(arxiv_id)
                done_ids.add(arxiv_id)
                append_manifest(arxiv_id, "ok", len(content_list), elapsed_per)
                stats["ok"] += 1
                total_new += 1

            if total_new % 20 == 0 and total_new > 0:
                rate = total_new / max(time.monotonic() - t_start, 1) * 60
                log.info(
                    "♥ +%d papers (%.1f/min) | +%d ok | %d total done",
                    total_new,
                    rate,
                    stats["ok"],
                    total_done + total_new,
                )

    # ── Summary ─────────────────────────────────────────────────────────────
    flush_checkpoint()
    elapsed = time.monotonic() - t_start
    log.info(
        "Node %s finished in %.1f min: ok=%d empty=%d extract=%d parse=%d save=%d",
        PET_NODE_RANK,
        elapsed / 60,
        stats["ok"],
        stats["empty"],
        stats["extract_failed"],
        stats["parse_failed"],
        stats["save_failed"],
    )


def main() -> None:
    _resolve_gpus()
    log.info(
        "Node %s/%s | GPUs: %s | Model: %s", PET_NODE_RANK, PET_NNODES, NUM_GPUS, MODEL_PATH.name
    )
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        log.info("Interrupted (SIGINT)")
    except asyncio.CancelledError:
        log.info("Cancelled (SIGTERM)")
    except Exception:
        log.exception("Fatal error")
    finally:
        log.info("Shutting down vLLM server")
        stop_vllm_server()
        log.info("Flushing checkpoint")
        flush_checkpoint()


atexit.register(flush_checkpoint)


if __name__ == "__main__":
    main()
