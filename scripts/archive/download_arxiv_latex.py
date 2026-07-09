#!/usr/bin/env python3
"""
download_arxiv_latex.py — 从 HuggingFace TIGER-Lab/arxiv-latex-5T 下载 arXiv LaTeX 源文件 tar

策略:
  逐个下载 tar (200-600MB) → 直接存到目标目录，不做任何解析。
  磁盘需求 ≈ 2.8TB（9547 个 tar × 平均 ~300MB）。

特性:
  - tar 列表本地缓存: 首次从 HF API 获取后存为 .tar_list.json，后续启动秒读
  - 断点续传: done_tars.txt + corrupt_tars/ 目录
  - 日志归档: 启动时自动归档旧日志，RotatingFileHandler 滚动（50MB×20）
  - 下载重试: 每个 tar 失败重试 3 次，指数退避 (10s → 20s → 40s)
  - 限速保护: 每次下载后 sleep N 秒避免触发 HF 429 限流
  - 进度显示: 实时 ETA、速度、下载量
  - 优雅中断: SIGINT/SIGTERM → 当前 tar 下载完即退出

用法:
  HF_TOKEN=hf_xxx python scripts/download_arxiv_latex.py                 # 全量 9544 tar
  HF_TOKEN=hf_xxx python scripts/download_arxiv_latex.py --max-tars 3    # 测试3个
  HF_TOKEN=hf_xxx python scripts/download_arxiv_latex.py --year 2024     # 只下2024年
  HF_TOKEN=hf_xxx python scripts/download_arxiv_latex.py --dry-run       # 预览

环境变量:
  HF_TOKEN             HuggingFace token (必需，否则共享 IP 会 429)
  SCHOLIGHT_DATA_ROOT    数据根目录 (默认 /inspire/qb-ilm/.../academic-data)
  SCHOLIGHT_LOG_LEVEL    日志级别 (默认 INFO)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import structlog
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
)

# ── Path setup ─────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scholight.logging import configure_logging

# ── CLI ────────────────────────────────────────────────────────────────────────
_parser = argparse.ArgumentParser(description="下载 arXiv LaTeX 源文件 tar (HF buf dataset)")
_parser.add_argument("--max-tars", type=int, default=0, help="最多下载 tar 数量 (0=全量)")
_parser.add_argument("--year", type=int, default=0, help="只下载指定年份 (如 2024)")
_parser.add_argument("--tar-pattern", type=str, default="", help="tar 文件名子串过滤")
_parser.add_argument("--dry-run", action="store_true", help="预览，不下载")
_parser.add_argument("--wait", type=float, default=5.0, help="每次下载后等待秒数 (默认5)")
_parser.add_argument("--refresh-list", action="store_true", help="强制刷新 HF 文件列表缓存")
_args = _parser.parse_args()

# ── Constants ──────────────────────────────────────────────────────────────────
REPO_ID = "TIGER-Lab/arxiv-latex-5T"
_HF_TOKEN = os.environ.get("HF_TOKEN", None)
# HF 镜像：避免 Xet 存储 429 限流（启智平台共享 IP 容易触发）
_HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")

DATA_ROOT = Path(
    os.environ.get(
        "SCHOLIGHT_DATA_ROOT",
        "/inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/academic-data",
    )
)
DOWNLOAD_DIR = Path(
    os.environ.get("SCHOLIGHT_DOWNLOAD_DIR", str(DATA_ROOT / "arxiv_latex_src" / "tars"))
)
LOG_DIR = DATA_ROOT / "logs" / "download_arxiv_latex"
CHECKPOINT_DIR = DOWNLOAD_DIR / ".checkpoints"
TAR_LIST_CACHE = CHECKPOINT_DIR / ".tar_list.json"
DONE_TARS_FILE = CHECKPOINT_DIR / "done_tars.txt"
CORRUPT_DIR = CHECKPOINT_DIR / "corrupt_tars"
MANIFEST_FILE = CHECKPOINT_DIR / "manifest.jsonl"

_YEAR_CUTOFF = 91  # arXiv YY: >= 91 → 19YY, < 91 → 20YY


# ── Shutdown ───────────────────────────────────────────────────────────────────
_shutdown_requested = False


def _on_shutdown(signum: int, _frame: object) -> None:
    global _shutdown_requested
    sig = signal.Signals(signum).name
    print(f"\n[download] {sig} — finishing current tar then exit", file=sys.stderr)
    _shutdown_requested = True


signal.signal(signal.SIGINT, _on_shutdown)
signal.signal(signal.SIGTERM, _on_shutdown)


# ── Logging ────────────────────────────────────────────────────────────────────
def _setup_logging() -> structlog.BoundLogger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "download.log"

    if log_file.exists():
        ts = datetime.fromtimestamp(log_file.stat().st_mtime, tz=UTC)
        archive = LOG_DIR / f"download_{ts.strftime('%Y%m%d_%H%M%S')}.log"
        shutil.move(str(log_file), str(archive))

    configure_logging(
        log_level=os.environ.get("SCHOLIGHT_LOG_LEVEL", "INFO"),
        use_json=os.environ.get("SCHOLIGHT_LOG_JSON") == "1" or not sys.stderr.isatty(),
        file_handler=(str(log_file), 50_000_000, 20),
    )
    return structlog.get_logger("download_arxiv_latex")


log = _setup_logging()


# ── Utility ────────────────────────────────────────────────────────────────────
def _stem(tar_name: str) -> str:
    """arXiv_src_2401_001.tar → arXiv_src_2401_001"""
    return tar_name.removesuffix(".tar")


def _tar_year(tar_name: str) -> int:
    """arXiv_src_2401_001 → 2024"""
    yy = int(tar_name[len("arXiv_src_") : len("arXiv_src_") + 2])
    return 1900 + yy if yy >= _YEAR_CUTOFF else 2000 + yy


def _dest_path(tar_name: str) -> Path:
    """arXiv_src_2401_001.tar → DOWNLOAD_DIR/2024/arXiv_src_2401_001.tar"""
    year = _tar_year(tar_name)
    sub = DOWNLOAD_DIR / str(year)
    sub.mkdir(parents=True, exist_ok=True)
    return sub / tar_name


# ── Checkpoint ─────────────────────────────────────────────────────────────────
class Checkpoint:
    def __init__(self) -> None:
        for d in (CHECKPOINT_DIR, CORRUPT_DIR, DOWNLOAD_DIR):
            d.mkdir(parents=True, exist_ok=True)

        self.done: set[str] = self._load_lines(DONE_TARS_FILE)
        self.corrupt: set[str] = {p.stem for p in CORRUPT_DIR.iterdir() if p.is_file()}

        log.info("checkpoint  done=%d  corrupt=%d", len(self.done), len(self.corrupt))

    @staticmethod
    def _load_lines(p: Path) -> set[str]:
        if not p.exists():
            return set()
        return {l.strip() for l in p.read_text().splitlines() if l.strip()}

    def mark_done(self, name: str) -> None:
        with open(DONE_TARS_FILE, "a") as f:
            f.write(f"{name}\n")
        self.done.add(name)

    def mark_corrupt(self, name: str) -> None:
        (CORRUPT_DIR / name).touch()
        self.corrupt.add(name)

    def write_manifest(self, rec: dict) -> None:
        with open(MANIFEST_FILE, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── Tar list (with local cache) ────────────────────────────────────────────────
def _fetch_tar_list_from_hf() -> list[str]:
    """从 HF 获取全量 tar 文件列表（带 retry）。"""
    from huggingface_hub import list_repo_files

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=15, min=15, max=120),
        before_sleep=lambda rs: log.warning(
            "retry list_tars  attempt=%d/%d  err=%s",
            rs.attempt_number,
            5,
            rs.outcome.exception() if rs.outcome else "?",
        ),
        reraise=True,
    )
    def _do() -> list[str]:
        log.info("listing HF repo  repo=%s", REPO_ID)
        files = list(list_repo_files(REPO_ID, repo_type="dataset", token=_HF_TOKEN))
        tars = sorted(f for f in files if f.endswith(".tar") and f.startswith("arXiv_src_"))
        log.info("fetched %d tar files from HF", len(tars))
        return tars

    return _do()


def get_tar_list(refresh: bool = False) -> list[str]:
    """获取 tar 列表：优先读本地缓存，如不存在或需要刷新则从 HF 获取。"""
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

    if not refresh and TAR_LIST_CACHE.exists():
        log.info("loading tar list from cache  path=%s", TAR_LIST_CACHE)
        data = json.loads(TAR_LIST_CACHE.read_text())
        tars = data["tars"]
        log.info("cache loaded  n=%d  fetched_at=%s", len(tars), data.get("fetched_at", "?"))
        return tars

    tars = _fetch_tar_list_from_hf()
    TAR_LIST_CACHE.write_text(
        json.dumps(
            {"tars": tars, "fetched_at": datetime.now(UTC).isoformat(), "count": len(tars)},
            ensure_ascii=False,
        )
    )
    log.info("tar list cached to %s", TAR_LIST_CACHE)
    return tars


def filter_tars(all_tars: list[str], cp: Checkpoint) -> list[str]:
    """过滤：年份 → pattern → 去 done/corrupt → max_tars。"""
    tars = list(all_tars)

    if _args.year > 0:
        yy = f"{_args.year % 100:02d}"
        prefix = f"arXiv_src_{yy}"
        tars = [f for f in tars if f.startswith(prefix)]
        log.info("year filter  year=%d  remaining=%d", _args.year, len(tars))

    if _args.tar_pattern:
        tars = [f for f in tars if _args.tar_pattern in f]
        log.info("pattern filter  pattern=%s  remaining=%d", _args.tar_pattern, len(tars))

    pending = [f for f in tars if _stem(f) not in cp.done and _stem(f) not in cp.corrupt]
    n_done = sum(1 for f in tars if _stem(f) in cp.done)
    n_corrupt = sum(1 for f in tars if _stem(f) in cp.corrupt)

    log.info(
        "after checkpoint  total=%d  done=%d  corrupt=%d  pending=%d",
        len(tars),
        n_done,
        n_corrupt,
        len(pending),
    )

    if _args.max_tars > 0:
        pending = pending[: _args.max_tars]
        log.info("max_tars limit  limit=%d", _args.max_tars)

    return pending


# ── Download ───────────────────────────────────────────────────────────────────
def _download_one_raw(tar_name: str) -> Path:
    """单次下载（不含 retry 逻辑）。使用 HF_ENDPOINT 镜像避免 Xet 429。"""
    from huggingface_hub import hf_hub_download

    dest = _dest_path(tar_name)

    if dest.exists() and dest.stat().st_size > 0:
        log.info("already on disk  tar=%s  size=%d", tar_name, dest.stat().st_size)
        return dest

    log.info("downloading  tar=%s  endpoint=%s", tar_name, _HF_ENDPOINT)
    t0 = time.monotonic()

    # 先下载到临时目录，成功后再 move 到最终位置
    tmp_dir = DOWNLOAD_DIR / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    tmp_path = hf_hub_download(
        repo_id=REPO_ID,
        filename=tar_name,
        repo_type="dataset",
        token=_HF_TOKEN,
        endpoint=_HF_ENDPOINT,
        local_dir=str(tmp_dir),
        local_files_only=False,
    )

    src = Path(tmp_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))

    elapsed = time.monotonic() - t0
    size_mb = dest.stat().st_size / 1_048_576
    speed = size_mb / elapsed if elapsed > 0 else 0
    log.info("download ok  tar=%s  size=%.0fMB  %.1fs  %.1fMB/s", tar_name, size_mb, elapsed, speed)
    return dest


def _download_one(tar_name: str) -> Path:
    """带 tenacity retry 的下载（最多 3 次，10s/20s/40s）。"""
    wrapped: Callable[[str], Path] = retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=10, min=10, max=60),
        before_sleep=lambda rs: log.warning(
            "retry download  tar=%s  attempt=%d/3  err=%s",
            rs.args[0] if rs.args else "?",
            rs.attempt_number,
            rs.outcome.exception() if rs.outcome else "?",
        ),
        reraise=True,
    )(_download_one_raw)
    return wrapped(tar_name)


# ── Progress ───────────────────────────────────────────────────────────────────
class Progress:
    def __init__(self, pending_count: int, cp: Checkpoint) -> None:
        self.total = pending_count + len(cp.done) + len(cp.corrupt)
        self.processed = len(cp.done) + len(cp.corrupt)
        self.ok = 0
        self.fail = 0
        self.bytes_down = 0
        self.t0 = time.monotonic()
        self._last_tick = 0.0

    def add_ok(self, size: int) -> None:
        self.processed += 1
        self.ok += 1
        self.bytes_down += size
        self._tick()

    def add_fail(self) -> None:
        self.processed += 1
        self.fail += 1
        self._tick()

    def _tick(self) -> None:
        now = time.monotonic()
        if now - self._last_tick < 15:
            return
        self._last_tick = now
        self._report()

    def _report(self) -> None:
        elapsed = time.monotonic() - self.t0
        run = self.ok + self.fail
        remain = self.total - self.processed
        tph = run / elapsed * 3600 if elapsed > 0 else 0
        eta_h = remain / tph if tph > 0 else float("inf")
        gb = self.bytes_down / 1_073_741_824

        log.info(
            "📦  %d/%d(%.1f%%)  +%d ok/%d fail  %.1ft/h  %.1fGB  ETA %.1fh",
            self.processed,
            self.total,
            self.processed / max(self.total, 1) * 100,
            self.ok,
            self.fail,
            tph,
            gb,
            eta_h,
        )

    def summary(self) -> None:
        elapsed = time.monotonic() - self.t0
        run = self.ok + self.fail
        log.info(
            "=" * 60
            + "\n  ok=%d  failed=%d  dl=%.1fGB  elapsed=%.1fh"
            + "\n  avg %.1f tars/h (incl. wait)"
            + "\n"
            + "=" * 60,
            self.ok,
            self.fail,
            self.bytes_down / 1_073_741_824,
            elapsed / 3600,
            run / max(elapsed / 3600, 0.001),
        )


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    cp = Checkpoint()

    # ── 获取 tar 列表（优先缓存）──
    all_tars = get_tar_list(refresh=_args.refresh_list)
    pending = filter_tars(all_tars, cp)

    if _args.dry_run:
        print(f"\n=== DRY RUN: {len(pending)} tars pending ===\n")
        from collections import Counter

        yc = Counter(_tar_year(f) for f in pending)
        for y in sorted(yc):
            print(f"  {y}: {yc[y]:5d} tars")
        total_est = len(pending) * 300
        print(f"\n  Estimated: ~{total_est / 1024:.1f} GB")
        print(f"  Download dir:  {DOWNLOAD_DIR}")
        print(f"  Checkpoint:    {CHECKPOINT_DIR}")
        print(f"  Log dir:       {LOG_DIR}")
        print(f"  Wait between:  {_args.wait}s\n")
        for f in pending[:12]:
            print(f"  → {f}")
        if len(pending) > 12:
            print(f"  ... and {len(pending) - 12} more")
        print()
        return

    if not pending:
        log.info("nothing to download — all done!")
        return

    log.info(
        "🚀 start  pending=%d  dest=%s  wait=%.0fs  token=%s",
        len(pending),
        DOWNLOAD_DIR,
        _args.wait,
        "yes" if _HF_TOKEN else "⚠️ NO — expect 429 rate limits!",
    )

    if not _HF_TOKEN:
        log.error("HF_TOKEN not set! Aborting — shared IP will be 429'd immediately.")
        sys.exit(1)

    progress = Progress(len(pending), cp)

    for i, tar_name in enumerate(pending):
        if _shutdown_requested:
            log.info("shutdown  processed=%d/%d", i, len(pending))
            break

        t0 = time.monotonic()
        try:
            dest = _download_one(tar_name)
            cp.mark_done(_stem(tar_name))
            cp.write_manifest(
                {
                    "tar": tar_name,
                    "status": "ok",
                    "size": dest.stat().st_size,
                    "elapsed": round(time.monotonic() - t0, 1),
                    "time": datetime.now(UTC).isoformat(),
                }
            )
            progress.add_ok(dest.stat().st_size)
        except Exception as exc:
            cp.mark_corrupt(_stem(tar_name))
            cp.write_manifest(
                {
                    "tar": tar_name,
                    "status": "corrupt",
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "elapsed": round(time.monotonic() - t0, 1),
                    "time": datetime.now(UTC).isoformat(),
                }
            )
            log.error(
                "download FAILED after retries  tar=%s  %s: %s",
                tar_name,
                type(exc).__name__,
                str(exc)[:200],
            )
            progress.add_fail()

        if _args.wait > 0 and not _shutdown_requested:
            time.sleep(_args.wait)

    progress.summary()
    log.info("download dir: %s", DOWNLOAD_DIR)


if __name__ == "__main__":
    main()
