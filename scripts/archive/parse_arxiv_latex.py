#!/usr/bin/env python3
"""
parse_arxiv_latex.py — 从 arXiv LaTeX tar 提取源文件到 latex_dir

流程:
  1. 扫描全部 9547 个 tar → 构建 {canonical_id: tar_path} 索引 (缓存)
  2. Cursor-scan Milvus: has_latex == false
  3. 交叉 → 按 tar 分组 → 解压 → 写 latex_dir → 标记 has_latex=true

特性:
  - 不污染 tar 数据目录 (latex_dir 只写，tar_dir 只读)
  - latex_dir 直放 .tex/.cls/.sty/.bib/.png，不嵌套
  - 日志归档 + RotatingFileHandler 滚动
  - 断点续传 done_ids.txt
  - 按 tar 分组批量处理：同一 tar 只打开一次

用法:
  HF_TOKEN=hf_xxx python scripts/parse_arxiv_latex.py              # 全量
  python scripts/parse_arxiv_latex.py --max-tars 5                 # 测试: 5个tar
  python scripts/parse_arxiv_latex.py --build-index-only           # 只建索引
  python scripts/parse_arxiv_latex.py --dry-run                    # 预览

环境变量:
  SCHOLIGHT_DATA_ROOT / SCHOLIGHT_MILVUS_IP_FILE / SCHOLIGHT_LOG_LEVEL
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import re
import shutil
import signal
import sys
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path

import structlog
from pymilvus import MilvusClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scholight.logging import configure_logging
from scholight.sources.arxiv import canonicalize_arxiv_id
from scholight.storage import storage
from scholight.store.ingest import update_arxiv_paper

# ── CLI ────────────────────────────────────────────────────────────────────────
_parser = argparse.ArgumentParser(
    description="从 arXiv LaTeX tar 提取源文件到 latex_dir，更新 has_latex flag"
)
_parser.add_argument("--max-tars", type=int, default=0, help="最多处理 tar 数 (0=全量)")
_parser.add_argument("--dry-run", action="store_true", help="预览，不处理")
_parser.add_argument("--build-index-only", action="store_true", help="只建索引")
_parser.add_argument("--force-reindex", action="store_true", help="强制重建索引")
_parser.add_argument("--tar-dir", type=str, default="", help="tar 目录")
_args = _parser.parse_args()

# ── Constants ──────────────────────────────────────────────────────────────────
DATA_ROOT = Path(
    os.environ.get(
        "SCHOLIGHT_DATA_ROOT",
        "/inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/academic-data",
    )
)
TAR_ROOT = Path(_args.tar_dir) if _args.tar_dir else DATA_ROOT / "arxiv_latex_src" / "tars"
LOG_DIR = DATA_ROOT / "logs" / "parse_arxiv_latex"
CKPT_DIR = DATA_ROOT / "arxiv_latex_parsed" / "checkpoints"
TAR_INDEX_PATH = CKPT_DIR / ".tar_index.json"
DONE_IDS_FILE = CKPT_DIR / "done_ids.txt"
MANIFEST_FILE = CKPT_DIR / "manifest.jsonl"

_OLD_RE = re.compile(r"([a-z][a-z-]+)(\d{7})")

_shutdown_requested = False


def _on_shutdown(signum: int, _frame: object) -> None:
    global _shutdown_requested
    sig = signal.Signals(signum).name
    print(f"\n[parse_arxiv_latex] {sig} — finishing, then exit", file=sys.stderr)
    _shutdown_requested = True


signal.signal(signal.SIGINT, _on_shutdown)
signal.signal(signal.SIGTERM, _on_shutdown)


# ── Logging ────────────────────────────────────────────────────────────────────
def _setup_logging() -> structlog.BoundLogger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    lf = LOG_DIR / "parse.log"
    if lf.exists():
        ts = datetime.fromtimestamp(lf.stat().st_mtime, tz=UTC)
        shutil.move(str(lf), str(LOG_DIR / f"parse_{ts.strftime('%Y%m%d_%H%M%S')}.log"))
    configure_logging(
        log_level=os.environ.get("SCHOLIGHT_LOG_LEVEL", "INFO"),
        use_json=os.environ.get("SCHOLIGHT_LOG_JSON") == "1" or not sys.stderr.isatty(),
        file_handler=(str(lf), 50_000_000, 20),
    )
    return structlog.get_logger("parse_arxiv_latex")


log = _setup_logging()


# ── ID utils ───────────────────────────────────────────────────────────────────
def _entry_to_canonical(entry_id: str) -> str | None:
    """tar 条目名 → canonical arXiv ID.

    "2401.00001"       → "2401.00001"
    "astro-ph0001001"  → "astro-ph/0001001"
    """
    r = canonicalize_arxiv_id(entry_id)
    if r:
        return r
    m = _OLD_RE.match(entry_id)
    if m:
        repaired = f"{m.group(1)}/{m.group(2)}"
        r = canonicalize_arxiv_id(repaired)
        if r:
            return r
    return None


def _to_entry_stem(canonical_id: str) -> str:
    """canonical → tar entry stem: "astro-ph/0001001" → "astro-ph0001001" """
    return canonical_id.replace("/", "")


# ── Tar index (forward scan, cached) ───────────────────────────────────────────
def _scan_one_tar(tar_path: Path) -> list[str]:
    """读一个 tar 的 TOC，返回所有 canonical ID 列表。"""
    ids: list[str] = []
    try:
        with tarfile.open(tar_path, "r") as tf:
            for m in tf:
                if m.isdir():
                    continue
                parts = m.name.split("/")
                if len(parts) < 2:
                    continue
                fname = parts[-1]
                for ext in (".gz", ".pdf"):
                    if fname.endswith(ext):
                        cid = _entry_to_canonical(fname[: -len(ext)])
                        if cid:
                            ids.append(cid)
                        break
    except tarfile.ReadError as exc:
        log.warning("corrupt tar, skipping  tar=%s  err=%s", tar_path.name, exc)
    return ids


def build_tar_index(tar_root: Path, force: bool = False) -> dict[str, str]:
    """正向扫描所有 tar 文件头 → canonical_id → str(tar_path) 映射。

    多进程并行加速。结果缓存到 TAR_INDEX_PATH。
    """
    CKPT_DIR.mkdir(parents=True, exist_ok=True)

    if not force and TAR_INDEX_PATH.exists():
        raw = json.loads(TAR_INDEX_PATH.read_text())
        idx = raw["mapping"]
        log.info("cached index loaded  ids=%d  built=%s", len(idx), raw.get("built_at"))
        return idx

    log.info("building tar index  root=%s", tar_root)
    t0 = time.monotonic()

    # 收集所有 tar 文件
    tar_files = sorted(tar_root.rglob("arXiv_src_*.tar"))
    log.info("found %d tar files", len(tar_files))

    idx: dict[str, str] = {}
    scanned = 0

    for tar_path in tar_files:
        ids = _scan_one_tar(tar_path)
        path_str = str(tar_path)
        for cid in ids:
            if cid not in idx:
                idx[cid] = path_str
        scanned += 1
        if scanned % 200 == 0:
            elapsed = time.monotonic() - t0
            rate = scanned / elapsed if elapsed > 0 else 0
            log.info(
                "indexing  scanned=%d/%d (%.0f%%)  unique=%d  rate=%.0f tars/s",
                scanned,
                len(tar_files),
                scanned / len(tar_files) * 100,
                len(idx),
                rate,
            )

    elapsed = time.monotonic() - t0
    log.info(
        "index built  tars=%d  unique_ids=%d  elapsed=%.1fs (%.0f tars/s)",
        scanned,
        len(idx),
        elapsed,
        scanned / elapsed if elapsed else 0,
    )

    TAR_INDEX_PATH.write_text(
        json.dumps(
            {"mapping": idx, "built_at": datetime.now(UTC).isoformat(), "total": len(idx)},
            ensure_ascii=False,
        )
    )
    log.info("index cached  path=%s", TAR_INDEX_PATH)
    return idx


# ── Checkpoint ─────────────────────────────────────────────────────────────────
class Checkpoint:
    def __init__(self) -> None:
        CKPT_DIR.mkdir(parents=True, exist_ok=True)
        self.done: set[str] = self._load(DONE_IDS_FILE)
        log.info("checkpoint  done=%d", len(self.done))

    @staticmethod
    def _load(p: Path) -> set[str]:
        if not p.exists():
            return set()
        return {l.strip() for l in p.read_text().splitlines() if l.strip()}

    def mark_done(self, aid: str) -> None:
        with open(DONE_IDS_FILE, "a") as f:
            f.write(f"{aid}\n")
        self.done.add(aid)

    def write_manifest(self, rec: dict) -> None:
        with open(MANIFEST_FILE, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── Milvus ─────────────────────────────────────────────────────────────────────
def _get_client() -> MilvusClient:
    ip_file = os.environ.get(
        "SCHOLIGHT_MILVUS_IP_FILE",
        "/inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/academic-data/milvus-data/milvus_ip.txt",
    )
    ip = Path(ip_file).read_text().strip()
    port = int(os.environ.get("SCHOLIGHT_MILVUS_PORT", "19530"))
    log.info("milvus  uri=http://%s:%s", ip, port)
    return MilvusClient(uri=f"http://{ip}:{port}")


def gather_no_latex_ids(client: MilvusClient, cp: Checkpoint) -> set[str]:
    """Cursor-scan has_latex==false → set of arxiv_id. Skip already done."""
    ids: set[str] = set()
    last_id = ""
    while True:
        f = f"has_latex == false and arxiv_id > '{last_id}'" if last_id else "has_latex == false"
        results = client.query(
            collection_name="arxiv_papers", filter=f, output_fields=["arxiv_id"], limit=10000
        )
        if not results:
            break
        for r in results:
            aid = r["arxiv_id"]
            if aid not in cp.done:
                ids.add(aid)
        last_id = results[-1]["arxiv_id"]
        if len(results) < 10000:
            break
    log.info("milvus  has_latex_false=%d (excl. done)", len(ids))
    return ids


# ── Extract ─────────────────────────────────────────────────────────────────────
def _extract_one(
    data: bytes,
    is_pdf: bool,
    canonical_id: str,
    target: Path,
) -> bool:
    """从 bytes 解一篇论文到 target。 不涉及 tar IO。"""
    target.mkdir(parents=True, exist_ok=True)

    if is_pdf:
        dest = target / "paper.pdf"
        if not dest.exists():
            dest.write_bytes(data)
        return True

    try:
        inner = gzip.decompress(data)
    except gzip.BadGzipFile:
        log.warning("bad gzip  id=%s", canonical_id)
        return False

    try:
        with tarfile.open(fileobj=io.BytesIO(inner), mode="r") as itf:
            for info in itf:
                if info.isdir():
                    continue
                bn = Path(info.name).name
                if bn.startswith("._") or bn in ("__MACOSX", ".DS_Store") or not bn:
                    continue
                dest = target / bn
                if dest.exists():
                    continue
                f = itf.extractfile(info)
                if f is None:
                    continue
                dest.write_bytes(f.read())
        return True
    except tarfile.ReadError as exc:
        log.warning("inner tar corrupt  id=%s  err=%s", canonical_id, exc)
        return False
    except Exception as exc:
        log.error("extract fail  id=%s  err=%s", canonical_id, exc)
        return False


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    # 1. build tar index
    tar_index = build_tar_index(TAR_ROOT, force=_args.force_reindex)

    if _args.build_index_only:
        log.info("--build-index-only done  ids=%d", len(tar_index))
        return

    # 2. load workload from Milvus
    client = _get_client()
    cp = Checkpoint()
    need_ids = gather_no_latex_ids(client, cp)

    # 3. intersect: 哪些 paper 有 tar → 按 tar 分组
    from collections import defaultdict

    tar_groups: dict[str, list[str]] = defaultdict(list)
    missing = 0
    for aid in need_ids:
        tp = tar_index.get(aid)
        if tp:
            tar_groups[tp].append(aid)
        else:
            missing += 1

    log.info(
        "intersect  need=%d  found=%d  missing=%d",
        len(need_ids),
        sum(len(v) for v in tar_groups.values()),
        missing,
    )

    if _args.dry_run:
        print(f"\n=== DRY RUN ===\n  has_latex=false: {len(need_ids)}")
        print(f"  with tar:       {sum(len(v) for v in tar_groups.values())}")
        print(f"  without tar:    {missing}")
        print(f"  tar groups:     {len(tar_groups)}")
        print(f"  Tar index:      {len(tar_index)} entries")
        print(f"  Log dir:        {LOG_DIR}")
        for tp, aids in list(tar_groups.items())[:5]:
            print(f"  {Path(tp).name}: {len(aids)} papers")
        return

    if not tar_groups:
        log.info("nothing to do")
        return

    # 4. 逐 tar 批量处理
    processed = 0
    success = 0
    fail = 0
    t0 = time.monotonic()

    tar_list = list(tar_groups.items())
    if _args.max_tars > 0:
        tar_list = tar_list[: _args.max_tars]

    for tp_str, aids in tar_list:
        if _shutdown_requested:
            break

        tp = Path(tp_str)

        # ── 一次打开 tar，建索引 + 保持句柄做随机读取 ──
        try:
            tf = tarfile.open(tp, "r")
        except tarfile.ReadError as exc:
            log.warning("corrupt tar  tar=%s  err=%s  papers=%d", tp.name, exc, len(aids))
            for aid in aids:
                cp.mark_done(aid)
                fail += 1
                processed += 1
            continue

        stem_to_info: dict[str, tarfile.TarInfo] = {}
        try:
            for m in tf.getmembers():
                if m.isdir():
                    continue
                parts = m.name.split("/")
                if len(parts) < 2:
                    continue
                fname = parts[-1]
                for ext in (".gz", ".pdf"):
                    if fname.endswith(ext):
                        stem_to_info[fname[: -len(ext)]] = m
                        break
        except tarfile.ReadError as exc:
            tf.close()
            log.warning("corrupt tar TOC  tar=%s  err=%s  papers=%d", tp.name, exc, len(aids))
            for aid in aids:
                cp.mark_done(aid)
                fail += 1
                processed += 1
            continue

        batch_ok: list[str] = []
        batch_fail = 0
        batch_total = 0

        for aid in aids:
            stem = _to_entry_stem(aid)
            member = stem_to_info.get(stem)
            if member is None:
                fail += 1
                cp.mark_done(aid)
                processed += 1
                continue

            is_pdf = member.name.endswith(".pdf")
            r = client.get(collection_name="arxiv_papers", ids=[aid], output_fields=["created"])
            created = r[0].get("created", "1970-01-01") if r else "1970-01-01"

            data_bytes = b""
            try:
                f = tf.extractfile(member)
                if f:
                    data_bytes = f.read()
            except (tarfile.ReadError, OSError, EOFError):
                pass

            ok = _extract_one(data_bytes, is_pdf, aid, storage.latex_dir(aid, created))
            batch_total += 1
            if ok:
                batch_ok.append(aid)
                cp.mark_done(aid)
                success += 1
            else:
                fail += 1
                batch_fail += 1
                cp.mark_done(aid)

            # 失败率 > 30% 且处理了至少 15 篇 → 跳过 tar 剩余
            if batch_total >= 15 and batch_fail / batch_total > 0.30:
                log.warning(
                    "fail rate %.0f%% (%d/%d) in %s — bulk-skipping %d remaining",
                    batch_fail / batch_total * 100,
                    batch_fail,
                    batch_total,
                    tp.name,
                    len(aids) - batch_total,
                )
                for remaining in aids[batch_total:]:
                    cp.mark_done(remaining)
                    fail += 1
                break
            processed += 1

        tf.close()

        if batch_ok:
            for aid in batch_ok:
                try:
                    update_arxiv_paper(aid, {"has_latex": True})
                except Exception as exc:
                    log.error("milvus update fail  id=%s  err=%s", aid, exc)
            log.info(
                "batch  tar=%s  papers=%d  ok=%d",
                tp.name,
                len(aids),
                len(batch_ok),
            )
        else:
            log.warning("batch all failed  tar=%s", tp.name)

        elapsed = time.monotonic() - t0
        rate = success / elapsed * 3600 if elapsed > 0 else 0
        log.info("📦  processed=%d  ok=%d  fail=%d  rate=%.0f/h", processed, success, fail, rate)

    elapsed = time.monotonic() - t0
    log.info(
        "=" * 60 + "\n  SUMMARY  ok=%d  fail=%d  elapsed=%.1fh" + "\n" + "=" * 60,
        success,
        fail,
        elapsed / 3600,
    )


if __name__ == "__main__":
    main()
