#!/usr/bin/env python3
"""import_pre2007.py — 从 arxiv_archive (staeiou/zenodo) 导入 1993-2018 年到 Milvus.

数据源:
  /inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/academic-data/arxiv_archive/
    arxiv_archive-v1.0.1.zip → per_year/YYYY.tsv  (1,480,220 papers, 1993–2018)

Kaggle 优先级: 默认跳过已有 arxiv_id 的论文 (Kaggle 数据质量更高).

用法:
  python scripts/import_pre2007.py                       # 全量 1993-2018 (跳过已有)
  python scripts/import_pre2007.py --only-year 1993 1994  # 只导指定年
  python scripts/import_pre2007.py --dry-run              # 预览
  python scripts/import_pre2007.py --no-skip-existing     # 覆盖已有论文
  python scripts/import_pre2007.py --max-papers 100       # 最多100篇
"""

from __future__ import annotations

import argparse
import asyncio
import io
import math
import os
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd
import structlog

_project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_project_root))

from compass.config import settings  # noqa: E402
from compass.logging import configure_logging  # noqa: E402
from compass.pipeline.embedder import Embedder  # noqa: E402
from compass.storage import storage  # noqa: E402
from compass.store.concurrent import insert_arxiv_papers_concurrent  # noqa: E402

# ── Logging ──────────────────────────────────────────────────────────────

_LOG_FILE = storage.log_path("pre2007_import", "import.log")
configure_logging(
    log_level=settings.log_level,
    use_json=True,
    file_handler=(str(_LOG_FILE), 50_000_000, 5),
)
logger = structlog.get_logger("pre2007-import")

import logging  # noqa: E402

_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setLevel(logging.WARNING)
_stderr_handler.setFormatter(
    logging.Formatter("[%(asctime)s] %(name)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")
)
logging.getLogger().addHandler(_stderr_handler)


# ── Constants ────────────────────────────────────────────────────────────

ZIP_PATH = Path(
    os.environ.get(
        "ARXIV_ARCHIVE_ZIP",
        "/inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/academic-data/arxiv_archive/arxiv_archive-v1.0.1.zip",
    )
)

_MIN_YEAR: int = 1993
_MAX_YEAR: int = 2018
_DEFAULT_EMBED_BATCH: int = 256
_DEFAULT_EMBED_CONCURRENCY: int = 16
_DEFAULT_WRITE_CONCURRENCY: int = 16
_DEFAULT_MILVUS_BATCH: int = 10000


# ── Byte-level truncation (same as import_kaggle_bulk.py) ────────────────


def _truncate_bytes(s: str, max_bytes: int) -> str:
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _safe_str(val: Any, default: str = "", max_bytes: int = 0) -> str:
    s = default if val is None or (isinstance(val, float) and math.isnan(val)) else str(val)
    if max_bytes > 0:
        s = _truncate_bytes(s, max_bytes)
    return s


# ── Row conversion ───────────────────────────────────────────────────────


def convert_row(row: pd.Series) -> dict[str, Any]:
    """Convert one arxiv_archive TSV row to Milvus paper dict."""
    # Authors: comma-separated → list, byte-truncated
    author_text = str(row.get("author_text", "") or "")
    authors = (
        [_truncate_bytes(a.strip(), 256) for a in author_text.split(",") if a.strip()]
        if author_text
        else []
    )

    # Categories: comma-separated → list
    cat_text = str(row.get("categories", "") or "")
    categories = [c.strip() for c in cat_text.split(",") if c.strip()] if cat_text else []

    from compass.sources.arxiv import canonicalize_arxiv_id

    aid = canonicalize_arxiv_id(row.get("arxiv_id"))
    if aid is None:
        logger.warning("invalid arxiv_id — skipping row", raw=row.get("arxiv_id"))
        return None

    return {
        "arxiv_id": aid,
        "title": _safe_str(row.get("title"), max_bytes=2048),
        "abstract": _safe_str(row.get("abstract"), max_bytes=16384),
        "authors": authors,
        "categories": categories,
        "created": _safe_str(row.get("created", ""), "", max_bytes=16),
        "updated": _safe_str(row.get("updated", ""), "", max_bytes=16),
        "version": 1,  # No version history in this dataset
        "updated_history": [],
        "license": "",
        "comments": _safe_str(row.get("comments"), max_bytes=8192),
        "doi": _safe_str(row.get("doi"), max_bytes=256),
        "journal_ref": "",
        "acm_class": _safe_str(row.get("acm_class"), max_bytes=256),
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


# ── Embedding ────────────────────────────────────────────────────────────


async def _embed_abstracts(
    papers: list[dict[str, Any]],
    batch_size: int = _DEFAULT_EMBED_BATCH,
    concurrency: int = _DEFAULT_EMBED_CONCURRENCY,
) -> None:
    abstracts = [p.get("abstract", "") or "" for p in papers]
    non_empty = [(i, t) for i, t in enumerate(abstracts) if t.strip()]
    if not non_empty:
        for p in papers:
            p["abstract_embedding"] = [0.0] * settings.embedding_dim
        return

    embedder = Embedder()
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

    for (i, _), vec in zip(non_empty, all_vecs):
        papers[i]["abstract_embedding"] = vec

    for p in papers:
        if not p["abstract_embedding"]:
            p["abstract_embedding"] = [0.0] * settings.embedding_dim


# ── Import one year ──────────────────────────────────────────────────────


async def import_year_from_zip(
    zip_path: Path,
    year: int,
    write_concurrency: int,
    embed_batch: int,
    embed_concurrency: int,
    chunk_size: int = 10000,
    existing_ids: set[str] | None = None,
) -> dict[str, int]:
    """Read one year's TSV from zip and import to Milvus.

    If *existing_ids* is provided, papers whose ``arxiv_id`` is in the set
    are skipped (Kaggle 优先级 — 已由 Kaggle 导入的数据质量更高).
    """
    t0 = time.monotonic()
    logger.info("import year start", year=year)

    with zipfile.ZipFile(zip_path, "r") as outer:
        # Find the inner per_year/YYYY.tsv.zip inside the outer zip
        inner_name = None
        for name in outer.namelist():
            if name.endswith(f"per_year/{year}.tsv.zip"):
                inner_name = name
                break

        if inner_name is None:
            logger.error("year tsv.zip not found in archive", year=year)
            return {"inserted": 0, "errors": 0, "skipped": 0}

        # Open outer → inner zip → inner tsv
        with outer.open(inner_name) as inner_zip_bytes, zipfile.ZipFile(inner_zip_bytes) as inner:
            tsv_names = [n for n in inner.namelist() if n.endswith(f"{year}.tsv")]
            if not tsv_names:
                logger.error("no tsv inside inner zip", year=year)
                return {"inserted": 0, "errors": 0, "skipped": 0}

            with inner.open(tsv_names[0]) as tsv_f:
                # NB: first column is empty (leading tab in header), drop it
                df = pd.read_csv(
                    io.TextIOWrapper(tsv_f, encoding="utf-8"),
                    sep="\t",
                    quoting=0,  # QUOTE_MINIMAL — abstracts may contain tabs
                    na_values=["", "NA", "None"],
                    keep_default_na=False,
                )
                # Drop leading empty column (tab-separated header starts with \t)
                if not df.columns[0]:
                    df = df.iloc[:, 1:]

    total_rows = len(df)
    logger.info("tsv loaded", year=year, rows=total_rows)

    inserted = 0
    errors = 0
    skipped = 0

    for chunk_start in range(0, total_rows, chunk_size):
        chunk_end = min(chunk_start + chunk_size, total_rows)
        chunk_df = df.iloc[chunk_start:chunk_end]
        rows = chunk_df.to_dict(orient="records")

        papers = []
        for row in rows:
            try:
                paper = convert_row(pd.Series(row))
                aid = paper.get("arxiv_id")
                if existing_ids and aid in existing_ids:
                    skipped += 1
                    continue
                papers.append(paper)
            except Exception:
                logger.exception(
                    "failed to convert row", arxiv_id=row.get("arxiv_id", "?"), year=year
                )
                errors += 1

        if not papers:
            continue

        # Embed
        try:
            await _embed_abstracts(papers, batch_size=embed_batch, concurrency=embed_concurrency)
        except Exception:
            logger.exception("embedding failed", year=year, start=chunk_start, end=chunk_end)
            errors += len(papers)
            continue

        # Write to Milvus
        for i in range(0, len(papers), _DEFAULT_MILVUS_BATCH):
            batch = papers[i : i + _DEFAULT_MILVUS_BATCH]
            try:
                result = insert_arxiv_papers_concurrent(batch, concurrency=write_concurrency)
                inserted += result.get("inserted", len(batch))
            except Exception:
                logger.exception(
                    "milvus write failed", year=year, start=chunk_start + i, count=len(batch)
                )
                errors += len(batch)

        elapsed = time.monotonic() - t0
        progress = min(chunk_end, total_rows)
        pct = progress / total_rows * 100
        rate = progress / elapsed if elapsed > 0 else 0
        logger.info(
            "chunk done",
            year=year,
            progress=f"{progress:,}/{total_rows:,} ({pct:.1f}%)",
            inserted=f"{inserted:,}",
            errors=f"{errors:,}",
            skipped=f"{skipped:,}",
            rate=f"{rate:,.0f} rows/s",
        )

    elapsed = time.monotonic() - t0
    logger.info(
        "import year done",
        year=year,
        inserted=f"{inserted:,}",
        errors=f"{errors:,}",
        skipped=f"{skipped:,}",
        elapsed=f"{elapsed:.0f}s",
        rate=f"{total_rows / elapsed:,.0f} rows/s" if elapsed > 0 else "N/A",
    )

    return {"inserted": inserted, "errors": errors, "skipped": skipped}


# ── CLI ──────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="import_pre2007 — 从 arxiv_archive 导入 1993-2018")
    parser.add_argument("--dry-run", action="store_true", help="预览 (不导入)")
    parser.add_argument("--only-year", type=int, nargs="+", help="只导入指定年份")
    parser.add_argument("--embed-batch", type=int, default=_DEFAULT_EMBED_BATCH)
    parser.add_argument("--embed-concurrency", type=int, default=_DEFAULT_EMBED_CONCURRENCY)
    parser.add_argument("--write-concurrency", type=int, default=_DEFAULT_WRITE_CONCURRENCY)
    parser.add_argument(
        "--max-papers",
        type=int,
        default=0,
        help="最多导入 N 篇论文后停止 (0=不限制)",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="不跳过已有论文 (默认跳过, Kaggle 优先级更高)",
    )
    args = parser.parse_args()

    if not ZIP_PATH.exists():
        logger.error("zip not found", path=str(ZIP_PATH))
        sys.exit(1)

    years = list(args.only_year) if args.only_year else list(range(_MIN_YEAR, _MAX_YEAR + 1))
    logger.info("import pre2007 start", years=years, zip=str(ZIP_PATH))

    if args.dry_run:
        logger.info("DRY RUN")
        with zipfile.ZipFile(ZIP_PATH, "r") as outer:
            for year in years:
                inner_name = None
                for name in outer.namelist():
                    if name.endswith(f"per_year/{year}.tsv.zip"):
                        inner_name = name
                        break
                if inner_name is None:
                    logger.warning("year not found in zip", year=year)
                    continue

                with (
                    outer.open(inner_name) as inner_zip_bytes,
                    zipfile.ZipFile(inner_zip_bytes) as inner,
                ):
                    tsv_names = [n for n in inner.namelist() if n.endswith(f"{year}.tsv")]
                    if not tsv_names:
                        logger.warning("tsv not found inside inner zip", year=year)
                        continue

                    with inner.open(tsv_names[0]) as f:
                        df = pd.read_csv(
                            io.TextIOWrapper(f, encoding="utf-8"),
                            sep="\t",
                            na_values=["", "NA", "None"],
                            keep_default_na=False,
                            nrows=5,
                        )
                    if not df.columns[0]:
                        df = df.iloc[:, 1:]

                sample = convert_row(df.iloc[0]) if len(df) > 0 else {}
                logger.info(
                    "preview",
                    year=year,
                    rows=len(df),
                    sample_id=sample.get("arxiv_id", "?"),
                    sample_created=sample.get("created", "?"),
                    sample_title=(sample.get("title", "")[:60]),
                    sample_authors=sample.get("authors", [])[:2],
                )
        return

    # Load all existing arxiv_ids for dedup (Kaggle 优先级)
    existing_ids: set[str] = set()
    if not args.no_skip_existing:
        from compass.store.client import get_client

        client = get_client()
        logger.info("loading existing arxiv_ids for dedup...")
        last_id = ""
        while True:
            flt = f"arxiv_id > '{last_id}'" if last_id else "arxiv_id != ''"
            rows = client.query("arxiv_papers", filter=flt, output_fields=["arxiv_id"], limit=10000)
            if not rows:
                break
            existing_ids.update(r["arxiv_id"] for r in rows)
            last_id = rows[-1]["arxiv_id"]
        logger.info("dedup set ready", existing=len(existing_ids))

    async def _run() -> None:
        total = {"inserted": 0, "errors": 0, "skipped": 0}
        global_t0 = time.monotonic()
        for year in years:
            result = await import_year_from_zip(
                ZIP_PATH,
                year,
                write_concurrency=args.write_concurrency,
                embed_batch=args.embed_batch,
                embed_concurrency=args.embed_concurrency,
                existing_ids=existing_ids if not args.no_skip_existing else None,
            )
            for k in total:
                total[k] += result.get(k, 0)
            logger.info(
                "cumulative",
                year=year,
                total_inserted=f"{total['inserted']:,}",
                total_errors=f"{total['errors']:,}",
                total_skipped=f"{total['skipped']:,}",
            )
        elapsed = time.monotonic() - global_t0
        logger.info(
            "import complete",
            total_inserted=f"{total['inserted']:,}",
            total_errors=f"{total['errors']:,}",
            total_skipped=f"{total['skipped']:,}",
            elapsed=f"{elapsed:.0f}s ({elapsed / 60:.0f}m)",
            rate=f"{total['inserted'] / elapsed:,.0f} papers/s" if elapsed > 0 else "N/A",
        )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
