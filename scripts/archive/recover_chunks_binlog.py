"""Recover arxiv_chunks from Milvus binlog Parquet → Zilliz BulkWriter Parquet.

Each segment: 0/(chunk_id PK), 1/(metadata 5 cols), 105/(dense), 106/(old sparse—skip)
content_bm25 excluded — Zilliz BM25 Function auto-populates from content_text.

Referential integrity: chunks are only written for arxiv_ids whose corresponding
paper has ``has_chunks=True`` in the already-recovered papers Parquet.
The full ``has_chunks`` index is built once at startup (full-population scan).

Usage::
    uv run python scripts/recover_chunks_binlog.py
"""

from __future__ import annotations

import sys
import time
from contextlib import suppress
from pathlib import Path

import numpy as np
import pandas as pd
import structlog

_proj = Path(__file__).resolve().parents[1]
if str(_proj) not in sys.path:
    sys.path.insert(0, str(_proj))

logger = structlog.get_logger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────

BINLOG = Path(
    "/inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/"
    "academic-data/milvus-data/storage/insert_log"
)
CHUNKS_SEG_DIR = BINLOG / "466630988348691343" / "466630988348691344"

OUT = Path("/inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/academic-data/zilliz-import")
OUT_CHUNKS = OUT / "arxiv_chunks"
OUT_PAPERS = OUT / "arxiv_papers"

_EMPTY_THRESHOLD: int = 2048
_ROWS_PER_BATCH: int = 25_000  # chunks: 25K rows ≈ 1.5 GB memory (∼2 KB/row)
_FLUSH_INTERVAL: int = 500_000  # commit every 500K rows


# ── has_chunks index (full-population) ─────────────────────────────────────────

_has_chunks_cache: set[str] | None = None


def _load_has_chunks() -> set[str]:
    """Full-population scan of papers Parquet for arxiv_ids with has_chunks=True."""
    global _has_chunks_cache
    if _has_chunks_cache is not None:
        return _has_chunks_cache
    papers: set[str] = set()
    for pf in sorted(OUT_PAPERS.rglob("*.parquet")):
        df = pd.read_parquet(pf)
        if "has_chunks" in df.columns:
            papers.update(df.loc[df["has_chunks"].astype(bool), "arxiv_id"].tolist())
    _has_chunks_cache = papers
    logger.info("has_chunks index built", total_papers=len(papers))
    return papers


def _should_recover(arxiv_id: str) -> bool:
    """Return True if the paper for *arxiv_id* has has_chunks=True."""
    return arxiv_id in _load_has_chunks()


# ── Schema ─────────────────────────────────────────────────────────────────────


def _import_schema():
    from pymilvus import CollectionSchema

    from scholight.store.schema import ARXIV_CHUNKS_SCHEMA

    fields = [f for f in ARXIV_CHUNKS_SCHEMA.fields if f.name != "content_bm25"]
    return CollectionSchema(fields=fields, description="arxiv_chunks import")


def _import_fields() -> frozenset[str]:
    return frozenset(f.name for f in _import_schema().fields)


# ── Row decoder ────────────────────────────────────────────────────────────────


def _decode_chunk_row(
    i: int, chunk_id: str, df1: pd.DataFrame | None, df105: pd.DataFrame | None, dim: int
) -> dict:
    row: dict = {"chunk_id": chunk_id}
    if df1 is not None:
        m = df1.iloc[i]
        row["arxiv_id"] = str(m.get("arxiv_id", "") or "")
        row["chunk_idx"] = int(m.get("chunk_idx", 0) or 0)
        row["content_text"] = str(m.get("content_text", "") or "")
    else:
        row["arxiv_id"] = ""
        row["chunk_idx"] = 0
        row["content_text"] = ""
    if df105 is not None:
        raw = df105.iloc[i, 0]
        row["content_embedding"] = (
            np.frombuffer(raw, dtype=np.float32).tolist() if isinstance(raw, bytes) else [0.0] * dim
        )
    else:
        row["content_embedding"] = [0.0] * dim
    return row


# ── Segment scanner ────────────────────────────────────────────────────────────


def _valid_segments(seg_dir: Path) -> list[Path]:
    valid: list[Path] = []
    for seg in sorted(seg_dir.iterdir()):
        if not seg.is_dir():
            continue
        f0_files = list((seg / "0").glob("*"))
        if not f0_files or f0_files[0].stat().st_size <= _EMPTY_THRESHOLD:
            continue
        valid.append(seg)
    return valid


# ── Main ───────────────────────────────────────────────────────────────────────


def recover_chunks(output_dir: Path) -> None:
    from pymilvus.bulk_writer import BulkFileType, LocalBulkWriter

    from scholight.config import settings

    # ── Pre-flight schema audit ──
    expected = _import_fields()
    dummy = _decode_chunk_row(0, "test", None, None, 1024)
    missing = expected - set(dummy)
    extra = set(dummy) - expected
    if missing or extra:
        raise ValueError(f"Schema mismatch: missing={sorted(missing)} extra={sorted(extra)}")
    logger.info("schema validated", fields=len(expected))

    # ── Build has_chunks referential index (full-population) ──
    has_chunks = _load_has_chunks()
    logger.info("referential index ready", has_chunks_papers=len(has_chunks))

    output_dir.mkdir(parents=True, exist_ok=True)

    writer = LocalBulkWriter(
        schema=_import_schema(), local_path=str(output_dir), file_type=BulkFileType.PARQUET
    )

    segments = _valid_segments(CHUNKS_SEG_DIR)
    logger.info("segments found", total=len(segments))

    try:
        from tqdm import tqdm

        iterator = tqdm(segments, desc="Recovering arxiv_chunks", unit="seg")
    except ImportError:
        iterator = iter(segments)

    seg_count = total_rows = flushed_rows = skipped_rows = 0
    row_hist: list[int] = []
    started = time.monotonic()

    for seg in iterator:
        f0 = next((seg / "0").glob("*"))
        df0 = pd.read_parquet(f0)
        n = len(df0)
        if n == 0:
            continue

        f1_files = list((seg / "1").glob("*"))
        df1 = pd.read_parquet(f1_files[0]) if f1_files else None

        f105_files = list((seg / "105").glob("*"))
        df105 = pd.read_parquet(f105_files[0]) if f105_files else None

        if df1 is not None and len(df1) != n:
            raise ValueError(f"Seg {seg.name}: field_0={n} field_1={len(df1)}")
        if df105 is not None and len(df105) != n:
            raise ValueError(f"Seg {seg.name}: field_0={n} field_105={len(df105)}")

        if df105 is not None:
            raw = df105.iloc[0, 0]
            if isinstance(raw, bytes) and len(raw) // 4 != settings.embedding_dim:
                raise ValueError(f"Seg {seg.name}: dim={len(raw) // 4} != {settings.embedding_dim}")

        seg_rows = seg_skip = 0
        for bs in range(0, n, _ROWS_PER_BATCH):
            be = min(bs + _ROWS_PER_BATCH, n)
            for i in range(bs, be):
                cid = str(df0["chunk_id"].iloc[i])
                row = _decode_chunk_row(i, cid, df1, df105, settings.embedding_dim)
                aid = row["arxiv_id"]
                if aid not in has_chunks:
                    seg_skip += 1
                    skipped_rows += 1
                    continue
                writer.append_row(row)
                seg_rows += 1
                total_rows += 1

        del df0, df1, df105
        seg_count += 1
        row_hist.append(seg_rows)

        if seg_skip > 0:
            logger.debug("skipped orphan chunks", seg=seg.name, skipped=seg_skip, kept=seg_rows)

        if total_rows - flushed_rows >= _FLUSH_INTERVAL:
            writer.commit()
            flushed_rows = total_rows
            logger.info("flush checkpoint", total_rows=total_rows, segments=seg_count)

        with suppress(Exception):
            iterator.set_postfix(rows=f"{total_rows:,}")

    writer.commit()
    elapsed = time.monotonic() - started

    logger.info(
        "recovery complete",
        segments=seg_count,
        total_rows=f"{total_rows:,}",
        skipped_orphans=f"{skipped_rows:,}",
        elapsed_min=round(elapsed / 60, 1),
        rows_per_sec=round(total_rows / max(elapsed, 0.1)),
    )
    if row_hist:
        logger.info(
            "segment stats",
            min=min(row_hist),
            max=max(row_hist),
            mean=round(sum(row_hist) / len(row_hist)),
        )


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    recover_chunks(OUT_CHUNKS)
