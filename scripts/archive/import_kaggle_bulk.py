#!/usr/bin/env python3
"""import_kaggle_bulk.py — 从 HuggingFace arxiv-metadata-snapshot 批量导入 Milvus.

数据源:
  /inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/academic-data/kaggle-arxiv/data/train-*.parquet
  (3,050,852 papers, 2007–2026)

流程:
  1. 读取 parquet 文件（流式分批避免 OOM）
  2. Embedding abstract（Qwen3-Embedding-0.6B，512 batch × 8 并发）
  3. 并发写入 Milvus (upsert, arxiv_id 为 PK)

用法:
  python scripts/import_kaggle_bulk.py                       # 全量导入
  python scripts/import_kaggle_bulk.py --only-year 2025     # 只导特定年份
  python scripts/import_kaggle_bulk.py --dry-run             # 预览不写库
  python scripts/import_kaggle_bulk.py --embed-batch 1024 --concurrency 16  # 调并发
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import glob
import logging
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd
import structlog

_project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_project_root))

from scholight.config import settings  # noqa: E402
from scholight.logging import configure_logging  # noqa: E402
from scholight.pipeline.embedder import Embedder  # noqa: E402
from scholight.storage import storage  # noqa: E402
from scholight.store.concurrent import insert_arxiv_papers_concurrent  # noqa: E402

_LOG_FILE = storage.log_path("kaggle_import", "import.log")

configure_logging(
    log_level=settings.log_level,
    use_json=True,
    file_handler=(str(_LOG_FILE), 50_000_000, 5),
)
logger = structlog.get_logger("kaggle-import")

# Also log to stderr for real-time observation
_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setLevel(logging.WARNING)
_stderr_fmt = logging.Formatter(
    "[%(asctime)s] %(name)s %(levelname)s: %(message)s", datefmt="%H:%M:%S"
)
_stderr_handler.setFormatter(_stderr_fmt)
logging.getLogger().addHandler(_stderr_handler)

# ── Constants ────────────────────────────────────────────────────────────

PARQUET_DIR = Path(
    os.environ.get(
        "KAGGLE_DIR",
        "/inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/academic-data/kaggle-arxiv/data",
    )
)
_PARQUET_GLOB = "train-*.parquet"

# Defaults — can be overridden via CLI or settings
_DEFAULT_EMBED_BATCH: int = 256  # 128 is too conservative for 64-core
_DEFAULT_EMBED_CONCURRENCY: int = 16
_DEFAULT_MILVUS_BATCH: int = 10000  # papers per concurrent write shard
_DEFAULT_WRITE_CONCURRENCY: int = 16
_DEFAULT_READ_CHUNK: int = 10000  # rows per parquet read

# arxiv_papers schema fields (all lowercase in Milvus)
PAPER_FIELDS: list[str] = [
    "arxiv_id",
    "title",
    "authors",
    "abstract",
    "categories",
    "created",
    "updated",
    "version",
    "updated_history",
    "license",
    "comments",
    "doi",
    "journal_ref",
    "acm_class",
]


# ── Row conversion ───────────────────────────────────────────────────────


def _parse_authors(row: dict[str, Any]) -> list[str]:
    """Extract authors from Kaggle parquet row.

    Prefers ``authors_parsed`` when available (``[[last, first, suffix], ...]``),
    falls back to parsing the ``authors`` string.
    """
    # Try authors_parsed first (list of [last, first, suffix] each)
    parsed = row.get("authors_parsed")
    if parsed is not None and hasattr(parsed, "__len__") and len(parsed) > 0:
        names: list[str] = []
        for entry in parsed:
            if hasattr(entry, "__len__") and len(entry) >= 2:
                last, first = str(entry[0]).strip(), str(entry[1]).strip()
                suffix = str(entry[2]).strip() if len(entry) >= 3 else ""
                full = _truncate_bytes(
                    f"{first} {last} {suffix}".rstrip(), 256
                )  # Milvus varchar(256 bytes)
                if full.strip():
                    names.append(full)
            else:
                s = str(entry).strip()
                if s:
                    names.append(_truncate_bytes(s, 256))
        if names:
            return names

    # Fallback: parse flat string
    raw = row.get("authors", "")
    if not raw or (isinstance(raw, float) and math.isnan(raw)):
        return []
    raw_str = str(raw)
    # Both " and " and ", " used as separators
    parts = re.split(r"\s+and\s+|,\s*|\s*;\s*", raw_str)
    return [
        _truncate_bytes(p.strip(), 256) for p in parts if p.strip()
    ]  # Milvus varchar(256 bytes)


def _parse_categories(row: dict[str, Any]) -> list[str]:
    raw = row.get("categories", "")
    if not raw or (isinstance(raw, float) and math.isnan(raw)):
        return []
    return str(raw).split()


def _truncate_bytes(s: str, max_bytes: int) -> str:
    """Truncate string so its UTF-8 encoded length does not exceed max_bytes.

    Milvus varchar/array elements are byte-limited, not character-limited.
    """
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _safe_str(val: Any, default: str = "", max_bytes: int = 0) -> str:
    s = default if val is None or (isinstance(val, float) and math.isnan(val)) else str(val)
    if max_bytes > 0:
        s = _truncate_bytes(s, max_bytes)
    return s


# Date format: "Mon, 25 Oct 2010 16:03:12 GMT" → "2010-10-25" (Milvus max_length=16)
_KAGGLE_DATE_RE = re.compile(r"\w{3},?\s*(\d{1,2})\s+(\w{3})\s+(\d{4})\s+")

_MONTH_MAP: dict[str, str] = {
    "Jan": "01",
    "Feb": "02",
    "Mar": "03",
    "Apr": "04",
    "May": "05",
    "Jun": "06",
    "Jul": "07",
    "Aug": "08",
    "Sep": "09",
    "Oct": "10",
    "Nov": "11",
    "Dec": "12",
}


def _normalize_date(d: str) -> str:
    """Convert Kaggle-style date to YYYY-MM-DD (fits Milvus varchar(16))."""
    m = _KAGGLE_DATE_RE.match(d)
    if m:
        day = m.group(1).zfill(2)
        month = _MONTH_MAP.get(m.group(2), "01")
        year = m.group(3)
        return f"{year}-{month}-{day}"
    # Fallback: try parsing as ISO
    try:
        return dt.date.fromisoformat(d[:10]).isoformat()
    except (ValueError, TypeError):
        return d[:16]  # best-effort truncate


def _parse_versions(row: dict[str, Any]) -> tuple[str, str, int, list[str]]:
    """Return (created, updated, version_count, version_dates) from versions field.

    Dates are normalized to YYYY-MM-DD for Milvus varchar(16).
    """
    versions = row.get("versions", [])
    if versions is None or not hasattr(versions, "__len__") or len(versions) == 0:
        return ("", "", 1, [])

    dates: list[str] = []
    for v in versions:
        if isinstance(v, dict):
            d = v.get("created", "")
            if d:
                dates.append(_normalize_date(d))

    created = dates[0] if dates else ""
    updated = dates[-1] if dates else ""
    vcount = len(versions)
    return (created, updated, vcount, dates)


def convert_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert one Kaggle parquet row to Milvus arxiv_papers dict."""
    created, updated, vcount, version_dates = _parse_versions(row)
    # noinspection PyUnresolvedReferences
    from scholight.sources.arxiv import canonicalize_arxiv_id

    aid = canonicalize_arxiv_id(row.get("id"))
    if aid is None:
        logger.warning("invalid arxiv_id — skipping row", raw=row.get("id"))
        return None

    return {
        "arxiv_id": aid,
        "title": _safe_str(row.get("title"), max_bytes=2048),
        "abstract": _safe_str(row.get("abstract"), max_bytes=16384),
        "authors": _parse_authors(row),
        "categories": _parse_categories(row),
        "created": created,
        "updated": updated,
        "version": vcount,
        "updated_history": version_dates,
        "license": _safe_str(row.get("license"), max_bytes=512),
        "comments": _safe_str(row.get("comments"), max_bytes=8192),
        "doi": _safe_str(row.get("doi"), max_bytes=256),
        "journal_ref": _safe_str(row.get("journal-ref"), max_bytes=2048),
        "acm_class": _safe_str(row.get("report-no", ""), max_bytes=256),
        # Embedding / resource placeholders
        "abstract_embedding": [],
        "abstract_sparse": {},
        "title_sparse": {},
        "has_latex": False,
        "has_pdf": False,
        "has_markdown": False,
        "has_content_list": False,
        "has_chunks": False,
        "images_count": 0,
    }


# ── Core pipeline ────────────────────────────────────────────────────────


async def _embed_abstracts(
    papers: list[dict[str, Any]],
    batch_size: int = _DEFAULT_EMBED_BATCH,
    concurrency: int = _DEFAULT_EMBED_CONCURRENCY,
) -> list[list[float]]:
    """Embed all abstracts in papers.  Returns list of vectors aligned with papers."""
    abstracts = [p.get("abstract", "") or "" for p in papers]
    non_empty = [(i, t) for i, t in enumerate(abstracts) if t.strip()]

    embedder = Embedder()

    # Use our own batching instead of embedder.embed_many to override batch_size
    sem = asyncio.Semaphore(concurrency)

    async def _batch(e: Embedder, texts: list[str]) -> list[list[float]]:
        async with sem:
            return await e.embed_batch(texts)

    async with embedder as e:
        texts = [t for _, t in non_empty]
        batches = [texts[i : i + batch_size] for i in range(0, len(texts), batch_size)]
        results = await asyncio.gather(*[_batch(e, b) for b in batches])

    all_vecs: list[list[float]] = []
    for r in results:
        all_vecs.extend(r)

    # Fill back
    for (i, _), vec in zip(non_empty, all_vecs):
        papers[i]["abstract_embedding"] = vec

    # Zero-vector for empty abstracts
    for p in papers:
        if not p["abstract_embedding"]:
            p["abstract_embedding"] = [0.0] * settings.embedding_dim

    return [p["abstract_embedding"] for p in papers]


async def import_file(
    filepath: Path,
    write_concurrency: int,
    embed_batch: int,
    embed_concurrency: int,
    read_chunk: int,
    resume_from: int = 0,
    max_papers: int = 0,
) -> dict[str, int]:
    """Import one parquet file into Milvus.  Returns {inserted, errors, skipped}."""
    logger.info("import file start", file=filepath.name)
    t0 = time.monotonic()

    df = pd.read_parquet(str(filepath))
    total_rows = len(df)
    logger.info("parquet loaded", file=filepath.name, rows=total_rows)

    if total_rows <= resume_from:
        logger.info("file already fully imported, skipping", file=filepath.name)
        return {"inserted": 0, "errors": 0, "skipped": total_rows}

    inserted = 0
    errors = 0
    skipped = resume_from

    # Process in chunks: read → convert → embed → write
    for chunk_start in range(resume_from, total_rows, read_chunk):
        chunk_end = min(chunk_start + read_chunk, total_rows)
        chunk_df = df.iloc[chunk_start:chunk_end]
        rows = chunk_df.to_dict(orient="records")

        # Convert rows to Milvus format
        papers = []
        for row in rows:
            try:
                paper = convert_row(row)
                papers.append(paper)
            except Exception:
                logger.exception("failed to convert row", arxiv_id=row.get("id", "?"))
                errors += 1

        if not papers:
            continue

        # Embed all abstracts
        try:
            await _embed_abstracts(papers, batch_size=embed_batch, concurrency=embed_concurrency)
        except Exception:
            logger.exception(
                "embedding failed for chunk",
                start=chunk_start,
                end=chunk_end,
                file=filepath.name,
            )
            errors += len(papers)
            continue

        # Write to Milvus in sub-batches via concurrent workers
        milvus_batch = _DEFAULT_MILVUS_BATCH
        for i in range(0, len(papers), milvus_batch):
            batch = papers[i : i + milvus_batch]
            try:
                result = insert_arxiv_papers_concurrent(batch, concurrency=write_concurrency)
                inserted += result.get("inserted", len(batch))
            except Exception:
                logger.exception(
                    "milvus write failed",
                    start=chunk_start + i,
                    count=len(batch),
                    file=filepath.name,
                )
                errors += len(batch)

        elapsed = time.monotonic() - t0
        progress = min(chunk_end, total_rows)
        pct = progress / total_rows * 100
        rate = progress / elapsed if elapsed > 0 else 0
        logger.info(
            "chunk done",
            file=filepath.name,
            progress=f"{progress:,}/{total_rows:,} ({pct:.1f}%)",
            inserted=f"{inserted:,}",
            errors=f"{errors:,}",
            rate=f"{rate:,.0f} rows/s",
        )

        if max_papers and inserted >= max_papers:
            logger.info("max-papers reached", max_papers=max_papers, inserted=inserted)
            break

    elapsed = time.monotonic() - t0
    logger.info(
        "import file done",
        file=filepath.name,
        inserted=f"{inserted:,}",
        errors=f"{errors:,}",
        skipped=f"{skipped:,}",
        elapsed=f"{elapsed:.0f}s",
        rate=f"{total_rows / elapsed:,.0f} rows/s",
    )

    return {"inserted": inserted, "errors": errors, "skipped": skipped}


# ── Filters ──────────────────────────────────────────────────────────────


def _filter_by_year(years: tuple[int, ...]) -> tuple[int, ...]:
    """Return file indices that contain papers from target years."""
    if not years:
        return ()
    result = set()
    for fpath in sorted(glob.glob(str(PARQUET_DIR / _PARQUET_GLOB))):
        df = pd.read_parquet(fpath, columns=["id"])
        # Extract 2-digit year from arXiv IDs like YYMM.NNNNN or YYMM.NNNNNvN
        yr_vals = df["id"].str.extract(r"^(\d{2})\d{2}")[0].astype(float)
        # Convert: 05-90 → 2005-2090, 91-99 → 1991-1999
        yr_vals = yr_vals.map(lambda y: int(2000 + y if y < 91 else 1900 + y))
        file_years = set(yr_vals.dropna().unique().astype(int))
        if file_years & set(years):
            basename = os.path.basename(fpath)
            idx = int(basename.replace("train-", "").replace("-of-00010.parquet", ""))
            result.add(idx)
    return tuple(sorted(result))


# ── CLI ──────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="import_kaggle_bulk — 从 arxiv-metadata-snapshot 批量导入 Milvus"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览模式: 转换但不写入 Milvus (embedding 也跳过)",
    )
    parser.add_argument(
        "--only-year",
        type=int,
        nargs="+",
        help="只导入指定年份 (如 --only-year 2024 2025)",
    )
    parser.add_argument(
        "--embed-batch",
        type=int,
        default=_DEFAULT_EMBED_BATCH,
        help=f"embedding batch size (默认 {_DEFAULT_EMBED_BATCH})",
    )
    parser.add_argument(
        "--embed-concurrency",
        type=int,
        default=_DEFAULT_EMBED_CONCURRENCY,
        help=f"embedding 并发数 (默认 {_DEFAULT_EMBED_CONCURRENCY})",
    )
    parser.add_argument(
        "--write-concurrency",
        type=int,
        default=_DEFAULT_WRITE_CONCURRENCY,
        help=f"Milvus write concurrency (默认 {_DEFAULT_WRITE_CONCURRENCY})",
    )
    parser.add_argument(
        "--read-chunk",
        type=int,
        default=_DEFAULT_READ_CHUNK,
        help=f"每批从 parquet 读取的行数 (默认 {_DEFAULT_READ_CHUNK})",
    )
    parser.add_argument(
        "--resume-from",
        type=int,
        default=0,
        help="从指定行号续传 (逐文件: 需配合文件路径)",
    )
    parser.add_argument(
        "--file-index",
        type=int,
        nargs="+",
        help="只处理指定序号的 parquet 文件 (0-9)",
    )
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="跳过 embedding (papers 已在 Milvus 中但缺向量时使用)",
    )
    parser.add_argument(
        "--max-papers",
        type=int,
        default=0,
        help="最多导入 N 篇论文后停止 (0=不限制)",
    )
    args = parser.parse_args()

    # Discover files
    all_files = sorted(glob.glob(str(PARQUET_DIR / _PARQUET_GLOB)))
    if not all_files:
        logger.error("no parquet files found", dir=str(PARQUET_DIR))
        sys.exit(1)

    logger.info(f"found {len(all_files)} parquet files", dir=str(PARQUET_DIR))

    # Apply filters
    files = all_files
    if args.only_year:
        indices = _filter_by_year(tuple(args.only_year))
        files = [
            f
            for f in all_files
            if any(f.endswith(f"train-{i:05d}-of-00010.parquet") for i in indices)
        ]
        if not files:
            logger.error("no files match year filter", years=args.only_year)
            sys.exit(1)
        logger.info("year filter applied", years=args.only_year, files=len(files))

    if args.file_index is not None:
        files = [f for f in all_files if int(Path(f).stem.split("-")[1]) in args.file_index]
        if not files:
            logger.error("no files match --file-index", indices=args.file_index)
            sys.exit(1)

    resume_from = args.resume_from

    if args.dry_run:
        # Preview mode: convert first 100 rows
        logger.info("DRY RUN — previewing first 100 rows per file")
        for fpath in files:
            df = pd.read_parquet(str(fpath))
            rows = df.head(100).to_dict(orient="records")
            papers = [convert_row(row) for row in rows]
            logger.info(
                "preview",
                file=Path(fpath).name,
                rows=len(papers),
                sample_id=papers[0]["arxiv_id"] if papers else "?",
                sample_title=papers[0]["title"][:60] if papers else "?",
            )
        return

    # Run import
    async def _run() -> None:
        total = {"inserted": 0, "errors": 0, "skipped": 0}
        global_t0 = time.monotonic()

        for i, fpath in enumerate(files):
            fn = Path(fpath).name
            logger.info(f"file {i + 1}/{len(files)}: {fn}")

            result = await import_file(
                Path(fpath),
                write_concurrency=args.write_concurrency,
                embed_batch=args.embed_batch,
                embed_concurrency=args.embed_concurrency,
                read_chunk=args.read_chunk,
                resume_from=0 if i > 0 else resume_from,
                max_papers=args.max_papers,
            )
            total["inserted"] += result["inserted"]
            total["errors"] += result["errors"]
            total["skipped"] += result["skipped"]

            logger.info(
                "file cumulative",
                files_done=i + 1,
                total_inserted=f"{total['inserted']:,}",
                total_errors=f"{total['errors']:,}",
            )

        elapsed = time.monotonic() - global_t0
        logger.info(
            "import complete",
            total_inserted=f"{total['inserted']:,}",
            total_errors=f"{total['errors']:,}",
            elapsed=f"{elapsed:.0f}s ({elapsed / 3600:.1f}h)",
            rate=f"{total['inserted'] / elapsed:,.0f} papers/s",
        )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
