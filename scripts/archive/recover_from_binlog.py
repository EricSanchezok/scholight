"""Recover arxiv_papers data from Milvus binlog Parquet files to Zilliz Cloud via BulkWriter.

Each binlog segment contains up to 5 field subdirectories:

- ``0/``   — primary key (``arxiv_id``)
- ``1/``   — all 19 metadata columns in one Parquet table
- ``120/`` — dense embedding (``abstract_embedding``, 1024-dim float32)
- ``121/`` — sparse embedding (was ``abstract_sparse`` — skip)
- ``122/`` — sparse embedding (was ``title_sparse`` — skip)

The BM25 field ``abstract_bm25`` is auto-populated by a Zilliz BM25 Function
and is intentionally **excluded** from the output Parquet.

Usage::

    uv run python scripts/recover_from_binlog.py papers  # arxiv_papers only
    uv run python scripts/recover_from_binlog.py all     # papers + chunks (chunks NYI)
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import structlog

# Ensure project root is on sys.path so compass.* imports resolve.
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

if TYPE_CHECKING:
    from pymilvus import CollectionSchema

logger = structlog.get_logger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────

BINLOG_BASE = Path(
    "/inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/"
    "academic-data/milvus-data/storage/insert_log"
)
PAPERS_SEG_DIR = BINLOG_BASE / "466630988348691338" / "466630988348691339"

OUTPUT_DIR = Path(
    "/inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/academic-data/zilliz-import"
)

# An empty Parquet file is roughly 1 024 bytes (header only).
_EMPTY_PARQUET_THRESHOLD: int = 2048

# Per-segment OOM guard.  Each row is ~6 KB (1024×float32 embedding + 200-500 B
# text), so 50 000 rows ≈ 300 MB — well within the containerʼs memory budget.
_ROWS_PER_BATCH: int = 50_000


# ── Protobuf array decoder ─────────────────────────────────────────────────────


def _decode_array(raw: object) -> list[str]:
    """Decode a Milvus ARRAY(VARCHAR) binary field to ``list[str]``.

    The wire format is::

        0x32 <total_len>  <repeated 0x0a><elem_len><elem_utf8_bytes> ...

    ``None`` and NaN are decoded as empty lists.  Corrupt length bytes
    (observed on a handful of ``authors`` fields across the arXiv binlog)
    trigger early exit from the decode loop; invalid UTF-8 bytes are
    replaced with U+FFFD.
    """
    if raw is None or (isinstance(raw, float) and np.isnan(raw)):
        return []
    if not isinstance(raw, bytes):
        return raw if isinstance(raw, list) else []
    items: list[str] = []
    pos = 2  # skip 0x32 marker + total_length byte
    while pos < len(raw):
        if raw[pos] != 0x0A:
            break
        pos += 1
        if pos >= len(raw):
            break
        strlen = raw[pos]
        pos += 1
        if strlen <= 0 or pos + strlen > len(raw):
            break
        items.append(raw[pos : pos + strlen].decode("utf-8", errors="replace"))
        pos += strlen
    return items


# ── Schema builder ─────────────────────────────────────────────────────────────


def _build_import_schema() -> CollectionSchema:
    """Return a CollectionSchema for arxiv_papers **without** abstract_bm25."""
    from pymilvus import CollectionSchema

    from compass.store.schema import ARXIV_PAPERS_SCHEMA

    fields = [f for f in ARXIV_PAPERS_SCHEMA.fields if f.name != "abstract_bm25"]
    return CollectionSchema(fields=fields, description="arxiv_papers import")


def _import_field_names() -> frozenset[str]:
    """Names of every field the BulkWriter schema expects (19 — no abstract_bm25)."""
    schema = _build_import_schema()
    return frozenset(f.name for f in schema.fields)


# ── Row decoder ────────────────────────────────────────────────────────────────


def _decode_row(
    i: int,
    arxiv_id: str,
    df1: pd.DataFrame | None,
    df120: pd.DataFrame | None,
    embedding_dim: int,
) -> dict[str, object]:
    """Decode row *i* from segment DataFrames.

    Every key produced must be a member of ``_import_field_names()``.
    Missing metadata defaults to a zero-value of the correct type so the
    BulkWriter never receives ``None``.
    """
    row: dict[str, object] = {"arxiv_id": arxiv_id}

    if df1 is not None:
        meta = df1.iloc[i]
        row["title"] = str(meta.get("title", "") or "")
        row["authors"] = _decode_array(meta.get("authors"))
        row["abstract"] = str(meta.get("abstract", "") or "")
        row["categories"] = _decode_array(meta.get("categories"))
        row["created"] = str(meta.get("created", "") or "")
        row["updated"] = str(meta.get("updated", "") or "")
        row["version"] = int(meta.get("version", 0) or 0)
        row["updated_history"] = _decode_array(meta.get("updated_history"))
        row["license"] = str(meta.get("license", "") or "")
        row["comments"] = str(meta.get("comments", "") or "")
        row["doi"] = str(meta.get("doi", "") or "")
        row["journal_ref"] = str(meta.get("journal_ref", "") or "")
        row["acm_class"] = str(meta.get("acm_class", "") or "")
        row["has_latex"] = bool(meta.get("has_latex", False))
        row["has_pdf"] = bool(meta.get("has_pdf", False))
        row["has_markdown"] = bool(meta.get("has_markdown", False))
        row["has_chunks"] = bool(meta.get("has_chunks", False))
    else:
        row["title"] = ""
        row["authors"] = []
        row["abstract"] = ""
        row["categories"] = []
        row["created"] = ""
        row["updated"] = ""
        row["version"] = 0
        row["updated_history"] = []
        row["license"] = ""
        row["comments"] = ""
        row["doi"] = ""
        row["journal_ref"] = ""
        row["acm_class"] = ""
        row["has_latex"] = False
        row["has_pdf"] = False
        row["has_markdown"] = False
        row["has_chunks"] = False

    if df120 is not None:
        raw = df120.iloc[i, 0]
        if isinstance(raw, bytes):
            row["abstract_embedding"] = np.frombuffer(raw, dtype=np.float32).tolist()
        else:
            row["abstract_embedding"] = [0.0] * embedding_dim
    else:
        row["abstract_embedding"] = [0.0] * embedding_dim

    return row


# ── Segment scanner ────────────────────────────────────────────────────────────


def _iter_valid_segments(seg_dir: Path) -> list[Path]:
    """Return sorted segment dirs that contain real field_0 data."""
    valid: list[Path] = []
    for seg in sorted(seg_dir.iterdir()):
        if not seg.is_dir():
            continue
        f0_files = list((seg / "0").glob("*"))
        if not f0_files:
            logger.debug("segment has no field_0", seg=seg.name)
            continue
        f0_path = f0_files[0]
        if f0_path.stat().st_size <= _EMPTY_PARQUET_THRESHOLD:
            logger.debug("segment field_0 is empty stub", seg=seg.name)
            continue
        valid.append(seg)
    return valid


# ── Integrity checks ───────────────────────────────────────────────────────────


def _validate_segment(
    seg_name: str,
    n0: int,
    df1: pd.DataFrame | None,
    df120: pd.DataFrame | None,
    embedding_dim: int,
    nulls: Counter[str],
) -> None:
    """Assert row-count alignment and collect per-field nullity statistics."""
    if df1 is not None and len(df1) != n0:
        raise ValueError(f"Segment {seg_name}: field_0 has {n0} rows but field_1 has {len(df1)}")
    if df120 is not None and len(df120) != n0:
        raise ValueError(
            f"Segment {seg_name}: field_0 has {n0} rows but field_120 has {len(df120)}"
        )

    if df1 is None:
        return

    # Nullity statistics (sampled — first 1000 rows)
    sample_n = min(n0, 1000)
    schema_fields = _import_field_names()
    for col in df1.columns:
        if col not in schema_fields:
            continue
        empty = 0
        for i in range(sample_n):
            val = df1.iloc[i, df1.columns.get_loc(col)]
            if (
                val is None
                or (isinstance(val, float) and np.isnan(val))
                or (isinstance(val, str) and val == "")
                or (isinstance(val, (bytes, list)) and len(val) == 0)
            ):
                empty += 1
        if empty > 0:
            nulls[col] += empty

    # Embedding dimension check (spot-check first row)
    if df120 is not None:
        raw = df120.iloc[0, 0]
        if isinstance(raw, bytes):
            decoded = len(raw) // 4
            if decoded != embedding_dim:
                raise ValueError(
                    f"Segment {seg_name}: expected {embedding_dim}-dim embedding, "
                    f"got {decoded}-dim ({len(raw)} bytes)"
                )


def _validate_import_schema_coverage() -> None:
    """Assert every field produced by ``_decode_row`` exists in the BulkWriter schema."""
    import_schema_names = _import_field_names()
    dummy = _decode_row(0, "test", df1=None, df120=None, embedding_dim=1024)
    decoded_names = set(dummy.keys())

    missing = decoded_names - import_schema_names
    if missing:
        raise ValueError(
            f"_decode_row produces fields not in the import schema: {sorted(missing)}. "
            f"Schema expects: {sorted(import_schema_names)}"
        )
    extra = import_schema_names - decoded_names
    if extra:
        raise ValueError(
            f"Import schema expects fields not produced by _decode_row: {sorted(extra)}. "
            f"_decode_row produces: {sorted(decoded_names)}"
        )

    logger.info(
        "import schema validated",
        schema_fields=len(import_schema_names),
        decode_fields=len(decoded_names),
        match=True,
    )


# ── Post-write verification ────────────────────────────────────────────────────


def _verify_output(output_dir: Path, expected_total: int) -> None:
    """Read back generated Parquet files — verify row count, columns, vector dim."""
    from compass.config import settings

    import_schema_names = _import_field_names()
    total_read: int = 0

    for parquet_file in sorted(output_dir.rglob("*.parquet")):
        df = pd.read_parquet(parquet_file)
        n = len(df)
        total_read += n

        file_cols = set(df.columns)
        missing = import_schema_names - file_cols
        extra = file_cols - import_schema_names
        if missing:
            raise ValueError(f"Parquet {parquet_file.name} missing columns: {sorted(missing)}")
        if extra:
            logger.warning("parquet has extra columns", file=parquet_file.name, extra=sorted(extra))

        if "abstract_embedding" in file_cols and n > 0:
            dim = len(df["abstract_embedding"].iloc[0])
            if dim != settings.embedding_dim:
                raise ValueError(
                    f"Parquet {parquet_file.name}: embedding dim {dim} ≠ {settings.embedding_dim}"
                )

        logger.debug("parquet verified", file=parquet_file.name, rows=n)

    if total_read != expected_total:
        raise ValueError(
            f"Row-count mismatch: BulkWriter reported {expected_total}, "
            f"but Parquet files contain {total_read} rows"
        )

    logger.info("output verification passed", parquet_rows=total_read, expected=expected_total)


# ── Main recovery ──────────────────────────────────────────────────────────────


def recover_papers(
    output_dir: Path,
    batch_size: int | None = None,
) -> None:
    """Recover all arxiv_papers binlog segments → BulkWriter Parquet.

    Parameters
    ----------
    output_dir : Path
        Directory where the BulkWriter will write Parquet files.
    batch_size : int | None
        Log progress every *batch_size* segments.  ``None`` means use tqdm.
    """
    from pymilvus.bulk_writer import BulkFileType, LocalBulkWriter

    from compass.config import settings

    _validate_import_schema_coverage()

    logger.info("recovery started", collection="arxiv_papers", output_dir=str(output_dir))

    output_dir.mkdir(parents=True, exist_ok=True)

    import_schema = _build_import_schema()
    writer = LocalBulkWriter(
        schema=import_schema,
        local_path=str(output_dir),
        file_type=BulkFileType.PARQUET,
    )

    segments = _iter_valid_segments(PAPERS_SEG_DIR)
    logger.info("segments found", total=len(segments))

    # Progress reporting
    if batch_size is not None:
        progress_interval: int = batch_size
        iterator = iter(segments)
    else:
        progress_interval = 0
        try:
            from tqdm import tqdm
        except ImportError:
            iterator = iter(segments)
        else:
            iterator = tqdm(segments, desc="Recovering arxiv_papers", unit="seg")

    segment_count = 0
    total_rows = 0
    row_count_history: list[int] = []
    nulls: Counter[str] = Counter()
    started = time.monotonic()

    for seg in iterator:  # type: ignore[union-attr]
        f0_path = next((seg / "0").glob("*"))
        df0 = pd.read_parquet(f0_path)
        n = len(df0)
        if n == 0:
            logger.warning("segment field_0 is empty DataFrame", seg=seg.name)
            continue

        f1_files = list((seg / "1").glob("*"))
        df1 = pd.read_parquet(f1_files[0]) if f1_files else None

        f120_files = list((seg / "120").glob("*"))
        df120 = pd.read_parquet(f120_files[0]) if f120_files else None

        _validate_segment(seg.name, n, df1, df120, settings.embedding_dim, nulls)

        # Batch decode to avoid OOM on giant segments
        for batch_start in range(0, n, _ROWS_PER_BATCH):
            batch_end = min(batch_start + _ROWS_PER_BATCH, n)
            for i in range(batch_start, batch_end):
                arxiv_id = str(df0["arxiv_id"].iloc[i])
                row = _decode_row(i, arxiv_id, df1, df120, settings.embedding_dim)
                writer.append_row(row)

        # Release segment DataFrames immediately
        del df0, df1, df120

        segment_count += 1
        total_rows += n
        row_count_history.append(n)

        if progress_interval > 0 and segment_count % progress_interval == 0:
            elapsed = time.monotonic() - started
            logger.info(
                "progress",
                segments=segment_count,
                total_rows=total_rows,
                elapsed_sec=round(elapsed, 1),
            )
        else:
            with suppress(Exception):
                iterator.set_postfix(rows=total_rows)  # type: ignore[attr-defined]

    writer.commit()
    elapsed = time.monotonic() - started

    _verify_output(output_dir, total_rows)

    _log_summary(
        segment_count=segment_count,
        total_rows=total_rows,
        elapsed=elapsed,
        output_files=writer.batch_files,
        output_dir=str(output_dir),
        nulls=nulls,
        row_count_history=row_count_history,
    )


# ── Summary ────────────────────────────────────────────────────────────────────


def _log_summary(
    *,
    segment_count: int,
    total_rows: int,
    elapsed: float,
    output_files: list[list[str]],
    output_dir: str,
    nulls: Counter[str],
    row_count_history: list[int],
) -> None:
    """Emit structured summary log and nullity warnings."""
    elapsed_min = elapsed / 60
    rate = total_rows / max(elapsed, 0.1)

    logger.info(
        "recovery complete",
        segments=segment_count,
        total_rows=total_rows,
        elapsed_sec=round(elapsed, 1),
        elapsed_min=round(elapsed_min, 1),
        rows_per_sec=round(rate, 1),
        output_files=len(output_files),
        output_dir=output_dir,
    )

    if row_count_history:
        logger.info(
            "segment stats",
            min_rows=min(row_count_history),
            max_rows=max(row_count_history),
            mean_rows=round(sum(row_count_history) / len(row_count_history)),
        )

    if nulls:
        logger.info("nullity report (sampled per-segment, top-10)", **dict(nulls.most_common(10)))
    else:
        logger.info("nullity report — no empty values detected in sample")


# ── CLI ────────────────────────────────────────────────────────────────────────


def _print_usage() -> None:
    usage = (
        "Usage: uv run python scripts/recover_from_binlog.py <target>\n"
        "  papers  — recover arxiv_papers\n"
        "  all     — recover arxiv_papers + arxiv_chunks\n"
        "  chunks  — recover arxiv_chunks only\n"
    )
    sys.stderr.write(usage)
    sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        _print_usage()

    target = sys.argv[1]

    if target in ("papers", "all"):
        recover_papers(OUTPUT_DIR / "arxiv_papers")
    elif target == "chunks":
        logger.error("chunks recovery is not yet implemented")
        sys.exit(1)
    else:
        sys.stderr.write(f"Unknown target: {target!r}\n")
        _print_usage()

    if target == "all":
        logger.error("chunks recovery is not yet implemented")
