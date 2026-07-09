"""Logical backup: export/restore collections via cursor-scan JSONL.

Online (no downtime).  Exports every scalar + vector field as gzipped
JSONL shards.  Covers ``arxiv_papers`` + ``arxiv_chunks``.

Designed for:
- Daily incremental backups
- Cross-environment data migration
- Audit / compliance requirements

Restore loads JSONL → upserts back into Zilliz Cloud.  Only works when the target
collection schema matches the exported data.


For *file-level* snapshots (disaster recovery), see the archived
``scripts/backup_milvus_data.py`` script.
"""

from __future__ import annotations

import gzip
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import structlog
from pymilvus import MilvusClient, MilvusException

from scholight.store.client import escape_sql
from scholight.store.fields import CHUNK_ALL_FIELDS, PAPER_ALL_FIELDS

logger = structlog.get_logger(__name__)

# ── Per-collection metadata ───────────────────────────────────────────────────

# Fields to project during export — sourced from fields.py, the single source of
# truth for schema fields.
_ARXIV_PAPERS_FIELDS: list[str] = list(PAPER_ALL_FIELDS)
_ARXIV_CHUNKS_FIELDS: list[str] = list(CHUNK_ALL_FIELDS)

_COLLECTION_META: dict[str, dict[str, object]] = {
    "arxiv_papers": {"pk": "arxiv_id", "fields": _ARXIV_PAPERS_FIELDS},
    "arxiv_chunks": {"pk": "chunk_id", "fields": _ARXIV_CHUNKS_FIELDS},
}

_ROWS_PER_SHARD = 100_000

# Cursor batch size: capped at 500 rows because `query` projects ALL fields
# including `abstract_embedding` (1024 float32s ≈ 4 KB/row).  >500 rows
# consistently exceeds Milvus's internal gRPC payload limit (~4 MB).
_QUERY_LIMIT = 500

# In-memory batch size before flushing a compressed shard to disk.
# Kept at 10k (vs 100k) to limit peak memory: ~4 KB/row × 10k ≈ 40 MB
# plus Python overhead — stays within ~200 MB even with heavy GC pressure.
_FLUSH_ROWS = 10_000


def _iter_rows(
    client: MilvusClient,
    collection: str,
    *,
    pk_field: str,
    output_fields: list[str],
) -> Iterator[dict[str, object]]:
    """Cursor-scan *collection* yielding every row lazily.

    Iterates on the primary-key field to avoid offset slowdown on large
    collections.  Callers must pass the correct *pk_field* for the
    collection (``"arxiv_id"`` for papers, ``"chunk_id"`` for chunks).
    """
    last_id = ""
    while True:
        flt = f"{pk_field} > '{escape_sql(last_id)}'" if last_id else f"{pk_field} != ''"
        try:
            rows = client.query(
                collection,
                filter=flt,
                output_fields=output_fields,
                limit=_QUERY_LIMIT,
            )
        except MilvusException:
            logger.exception(
                "cursor query failed",
                collection=collection,
                last_id=last_id,
            )
            raise
        if not rows:
            break
        yield from rows
        last_id = rows[-1].get(pk_field, "")


def export_collection_to_path(
    client: MilvusClient,
    collection: str,
    output_dir: Path,
) -> int:
    """Export *collection* into ``output_dir/<shard_N>.jsonl.gz`` shards.

    Returns total number of rows written.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    total = 0
    shard_idx = 0

    meta = _COLLECTION_META.get(collection)
    if meta is None:
        raise ValueError(f"Unknown collection: {collection}")

    pk_field = cast(str, meta["pk"])
    fields = cast(list[str], meta["fields"])

    buf: list[str] = []
    for row in _iter_rows(client, collection, pk_field=pk_field, output_fields=fields):
        buf.append(json.dumps(row, default=_json_default))
        if len(buf) >= _ROWS_PER_SHARD:
            _flush_shard(output_dir, shard_idx, buf)
            total += len(buf)
            shard_idx += 1
            buf.clear()
            logger.info("backup shard written", shard=shard_idx, total=total)
        elif len(buf) % _FLUSH_ROWS == 0:
            logger.info("backup progress", collection=collection, so_far=total + len(buf))

    if buf:
        _flush_shard(output_dir, shard_idx, buf)
        total += len(buf)
        shard_idx += 1

    elapsed = time.perf_counter() - t0
    logger.info(
        "backup complete",
        collection=collection,
        total=total,
        shards=shard_idx,
        elapsed_s=round(elapsed, 1),
    )
    return total


def restore_collection_from_path(
    client: MilvusClient,
    collection: str,
    input_dir: Path,
    batch_size: int = 1000,
) -> int:
    """Restore *collection* from ``*.jsonl.gz`` files in *input_dir*.

    Reads shards in order, upserts in *batch_size* batches.
    Returns total rows inserted.
    """
    shards = sorted(input_dir.glob("shard_*.jsonl.gz"))
    if not shards:
        raise FileNotFoundError(f"No shard_*.jsonl.gz files found in {input_dir}")

    total = 0
    batch: list[dict[str, object]] = []

    errors = 0
    for shard_path in shards:
        with gzip.open(shard_path, "rt", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    errors += 1
                    logger.warning(
                        "restore: skipping corrupt line",
                        shard=shard_path.name,
                        lineno=lineno,
                    )
                    continue
                batch.append(_fix_sparse_keys(row))
                if len(batch) >= batch_size:
                    _upsert_batch(client, collection, batch)
                    total += len(batch)
                    batch.clear()

        logger.info("restore shard done", shard=shard_path.name, total=total)

    if batch:
        _upsert_batch(client, collection, batch)
        total += len(batch)

    logger.info("restore complete", collection=collection, total=total, errors=errors)
    if errors:
        logger.warning("restore: %d corrupt lines skipped — data may be incomplete", errors)
    return total


# ── Internal helpers ─────────────────────────────────────────────────────────


def _flush_shard(output_dir: Path, idx: int, rows: list[str]) -> None:
    path = output_dir / f"shard_{idx:04d}.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(rows))
        fh.write("\n")


def _upsert_batch(client: MilvusClient, collection: str, batch: list[dict[str, object]]) -> None:
    try:
        client.upsert(collection, data=batch, consistency_level="Strong")
    except MilvusException:
        logger.exception(
            "upsert failed during restore",
            collection=collection,
            count=len(batch),
        )
        raise


def _json_default(obj: object) -> list[object] | str | object:
    """Convert non-standard types to JSON-safe values.

    Handles pymilvus ``RepeatedScalarContainer`` (C-level protobuf ARRAY
    fields whose ``__iter__`` is invisible to ``hasattr``), ``bytes``, and
    ``set`` values.
    """
    # RepeatedScalarContainer / RepeatedCompositeContainer — C-level
    # protobuf types that support iteration but not hasattr(__iter__).
    type_name = type(obj).__name__
    if type_name.startswith("Repeated") and hasattr(obj, "__len__"):
        return list(obj)  # type: ignore[no-any-return,call-overload]
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, set):
        return list(obj)
    raise TypeError(f"Object of type {type_name} is not JSON serializable")


def _fix_sparse_keys(row: dict[str, object]) -> dict[str, object]:
    """Convert sparse vector string keys back to int after JSON roundtrip.

    JSON serializes ``{0: 0.823, 15: 0.456}`` as ``{"0": 0.823, "15": 0.456}``.
    Milvus ``SPARSE_FLOAT_VECTOR`` fields require ``dict[int, float]``.
    """
    fixed: dict[str, object] = {}
    for key, value in row.items():
        if isinstance(value, dict) and value:
            # Check if this looks like a sparse vector (all keys look numeric)
            sample = next(iter(value))
            if isinstance(sample, str) and sample.lstrip("-").isdigit():
                fixed[key] = {int(k): float(v) for k, v in value.items()}
            else:
                fixed[key] = value
        else:
            fixed[key] = value
    return fixed
