"""Zilliz Cloud data ingestion for arxiv_papers and arxiv_chunks.

All write operations acquire ``_WRITE_LOCK`` for thread safety.
Scale targets: 100M+ papers, 100M+ chunks.  Batch inserts capped at 1000 rows.
"""

from __future__ import annotations

from typing import Any, cast

import structlog
from pymilvus import MilvusClient
from pymilvus.exceptions import MilvusException

from scholight.store.client import (
    _WRITE_LOCK,
    DELETE_CONSISTENCY,
    QUERY_CONSISTENCY,
    batched,
    escape_sql,
    get_client,
)
from scholight.store.fields import (
    CHUNK_ALL_FIELDS,
    PAPER_ALL_FIELDS,
    PAPER_VECTOR_FIELDS,
)

logger = structlog.get_logger(__name__)

_BATCH_SIZE: int = 1000


# ── Custom exceptions ──────────────────────────────────────────────────────────


class StoreError(Exception):
    """Base exception for all store-layer errors."""


# ── Internal helpers ───────────────────────────────────────────────────────────


def _delete_chunks_locked(client: MilvusClient, arxiv_id: str) -> int:
    """Delete all arxiv chunks for *arxiv_id*.  Caller MUST hold ``_WRITE_LOCK``."""
    safe_id = escape_sql(arxiv_id)
    flt = f"arxiv_id == '{safe_id}'"
    try:
        result = client.delete("arxiv_chunks", filter=flt, consistency_level=DELETE_CONSISTENCY)
    except MilvusException as exc:
        logger.error("failed to delete arxiv chunks", arxiv_id=arxiv_id, error=str(exc))
        raise StoreError(f"Failed to delete arxiv chunks for arxiv_id={arxiv_id}: {exc}") from exc
    count: int = result.get("delete_count", 0)
    if count:
        logger.info("arxiv chunks deleted", arxiv_id=arxiv_id, count=count)
    return count


_PAPER_ALL_NON_VECTOR: frozenset[str] = frozenset(
    f for f in PAPER_ALL_FIELDS if f not in PAPER_VECTOR_FIELDS
)


def _validate_paper_full_upsert(paper: dict[str, Any]) -> None:
    """Guard for full-row upsert: refuse any dict that doesn't cover every field.

    A full ``upsert`` without ``partial_update=True`` replaces the **entire**
    Zilliz Cloud row.  Any field absent from the dict is set to its schema default
    (empty string, false, 0, etc.), silently wiping existing data.  This guard
    catches that at the call site before the write.

    For **partial** updates use :func:`update_arxiv_paper` instead, which
    sends ``partial_update=True`` and only touches the fields you specify.
    """
    arxiv_id = paper.get("arxiv_id", "?")
    paper_keys = set(paper)

    # 0. Validate arxiv_id format — catches non-canonical IDs at the last mile.
    from scholight.sources.arxiv import canonicalize_arxiv_id

    if canonicalize_arxiv_id(str(arxiv_id)) is None:
        raise StoreError(
            f"Paper {arxiv_id!r}: non-canonical arxiv_id — rejected by canonicalize_arxiv_id"
        )

    # 1. Every non-vector scalar field must be present.
    missing_scalars = _PAPER_ALL_NON_VECTOR - paper_keys
    if missing_scalars:
        raise StoreError(
            f"Paper {arxiv_id!r}: full upsert requires all scalar fields, "
            f"missing: {sorted(missing_scalars)}. "
            f"For partial updates use update_arxiv_paper() instead."
        )

    # 2. Every vector field must be present AND non-empty.
    for field in PAPER_VECTOR_FIELDS:
        val = paper.get(field)
        # val is None → key is entirely absent from the dict
        # val is [] or {} → key is present but empty
        if val is None:
            raise StoreError(
                f"Paper {arxiv_id!r}: vector field {field!r} is missing — "
                f"full upsert would wipe existing vectors. "
                f"Use partial_update=True or provide a valid vector."
            )
        if isinstance(val, (list, dict)) and len(val) == 0:
            raise StoreError(
                f"Paper {arxiv_id!r}: vector field {field!r} is empty — "
                f"full upsert would wipe existing vectors. "
                f"Use partial_update=True or provide a valid vector."
            )


def _validate_chunk_insert(chunk: dict[str, Any]) -> None:
    """Guard for chunk insert: refuse any chunk missing required fields.

    Called before ``client.insert`` — validates that ``chunk_id``, ``arxiv_id``,
    and ``content_embedding`` are present and non-empty.  BM25
    (``content_bm25``) is auto-populated by Zilliz Function and does not need
    to be supplied by the caller.
    """
    chunk_id = chunk.get("chunk_id")

    # 1. chunk_id must be a non-empty string.
    if not chunk_id or not isinstance(chunk_id, str) or not chunk_id.strip():
        if chunk_id is not None and not isinstance(chunk_id, str):
            raise StoreError(
                f"Chunk {chunk_id!r}: chunk_id must be str, got {type(chunk_id).__name__}"
            )
        raise StoreError(f"Chunk {chunk_id!r}: chunk_id is missing or empty")

    # 2. arxiv_id must be present and non-empty.
    arxiv_id = chunk.get("arxiv_id", "")
    if not arxiv_id or not isinstance(arxiv_id, str) or not arxiv_id.strip():
        raise StoreError(f"Chunk {chunk_id!r}: arxiv_id is missing or empty")

    # 3. content_embedding must be present AND non-empty.
    emb = chunk.get("content_embedding")
    if emb is None:
        raise StoreError(
            f"Chunk {chunk_id!r}: content_embedding is missing — "
            f"inserting a zero vector degrades future search quality."
        )
    if isinstance(emb, list) and len(emb) == 0:
        raise StoreError(
            f"Chunk {chunk_id!r}: content_embedding is empty — "
            f"inserting a zero vector degrades future search quality."
        )


# ── Paper ingestion ────────────────────────────────────────────────────────────


def insert_arxiv_paper(paper: dict[str, Any]) -> dict[str, Any]:
    """Upsert a single paper (arxiv_id is PK) — **full-row replacement**.

    .. warning::

       This is a **full** upsert (no ``partial_update``).  The *paper* dict
       must include every field defined in the collection schema, especially all
       vector fields (``abstract_embedding``, ``abstract_bm25`` — BM25
       Function auto-populated).  Missing or empty vector fields will silently
       wipe existing vectors.  For updating individual fields without touching
       vectors, use :func:`update_arxiv_paper` instead.
    """
    client = get_client()
    arxiv_id = paper.get("arxiv_id", "<unknown>")
    _validate_paper_full_upsert(paper)
    try:
        with _WRITE_LOCK:
            result = client.upsert("arxiv_papers", data=[paper], consistency_level="Strong")
    except MilvusException as exc:
        logger.error("failed to upsert paper", arxiv_id=arxiv_id, error=str(exc))
        raise StoreError(f"Failed to upsert paper arxiv_id={arxiv_id}: {exc}") from exc
    logger.info("paper upserted", arxiv_id=arxiv_id)
    return cast("dict[str, Any]", result)


def update_arxiv_paper(arxiv_id: str, fields: dict[str, Any]) -> bool:
    """Update only *fields* on *arxiv_id* via Zilliz Cloud partial_update.

    Uses ``partial_update=True`` — only the primary key and
    *fields* are sent.  Vector embeddings and all other columns are left
    untouched.  This is both faster and safer than the old read→merge→upsert
    pattern, because it avoids a race window where a concurrent writer could
    overwrite the fields set by *this* call.
    """
    client = get_client()
    data = {"arxiv_id": arxiv_id, **fields}
    try:
        with _WRITE_LOCK:
            result = client.upsert(
                "arxiv_papers",
                data=[data],
                partial_update=True,
                consistency_level="Strong",
            )
        upserted = result.get("upsert_count", 0)
        if upserted == 0:
            return False
    except MilvusException as exc:
        logger.error("failed to update paper", arxiv_id=arxiv_id, error=str(exc))
        raise StoreError(f"Failed to update paper arxiv_id={arxiv_id}: {exc}") from exc
    logger.info("paper updated", arxiv_id=arxiv_id)
    return True


def upsert_arxiv_papers(papers: list[dict[str, Any]]) -> dict[str, Any]:
    """Batch upsert — arxiv_id is PK, later entries overwrite earlier.

    .. warning::

       This is a **full-row** upsert.  Every paper dict must include all
       schema fields — especially vector fields — or existing vectors will be
       wiped.  For partial updates use :func:`update_arxiv_paper`.

    Split into 1000-row batches.  On mid-batch failure the exception includes
    ``upserted`` so the caller knows how many rows committed before the error
    (useful when the caller wants to skip already-written rows on retry).
    """
    if not papers:
        return {"inserted": 0, "updated": 0, "total": 0}
    client = get_client()
    total = len(papers)
    upserted = 0
    _batch_idx = 0
    batches = list(batched(papers))
    try:
        with _WRITE_LOCK:
            for _batch_idx, batch in enumerate(batches, start=1):
                for p in batch:
                    _validate_paper_full_upsert(p)
                result = client.upsert("arxiv_papers", data=batch, consistency_level="Strong")
                upserted += result.get("upsert_count", len(batch))
    except MilvusException as exc:
        logger.error(
            "failed to upsert arxiv papers",
            total=total,
            upserted=upserted,
            failed_batch=f"{_batch_idx}/{len(batches)}",
            error=str(exc),
        )
        raise StoreError(
            f"Upsert {total} papers failed at batch {_batch_idx}: "
            f"{upserted}/{total} committed before error ({exc})"
        ) from exc
    logger.info("arxiv papers upserted", total=total, upserted=upserted)
    return {"inserted": upserted, "updated": 0, "total": total}


def delete_arxiv_paper(arxiv_id: str) -> bool:
    """Delete a paper and all of its chunks.

    Warning: Deletion is not atomic across collections.  Chunks are deleted
    first; if the process crashes or Milvus becomes unreachable between the
    chunk-delete and paper-delete steps, the paper row survives but its
    chunks are permanently gone.  Callers that require atomicity must
    implement two-phase deletion at the application level.
    """
    client = get_client()
    safe_id = escape_sql(arxiv_id)
    try:
        with _WRITE_LOCK:
            _delete_chunks_locked(client, arxiv_id)
            rows = client.query(
                "arxiv_papers",
                filter=f"arxiv_id == '{safe_id}'",
                output_fields=["arxiv_id"],
                consistency_level=QUERY_CONSISTENCY,
                limit=1,
            )
            if not rows:
                logger.debug("paper not found for delete", arxiv_id=arxiv_id)
                return False
            client.delete(
                "arxiv_papers",
                filter=f"arxiv_id == '{safe_id}'",
                consistency_level=DELETE_CONSISTENCY,
            )
    except MilvusException as exc:
        logger.error("failed to delete paper", arxiv_id=arxiv_id, error=str(exc))
        raise StoreError(f"Failed to delete paper arxiv_id={arxiv_id}: {exc}") from exc
    logger.info("paper deleted", arxiv_id=arxiv_id)
    return True


def get_arxiv_paper(arxiv_id: str) -> dict[str, Any] | None:
    """Return a paper dict by *arxiv_id* (all fields including vectors), or None."""
    client = get_client()
    safe_id = escape_sql(arxiv_id)
    try:
        rows = client.query(
            "arxiv_papers",
            filter=f"arxiv_id == '{safe_id}'",
            output_fields=list(PAPER_ALL_FIELDS),
            consistency_level=QUERY_CONSISTENCY,
            limit=1,
        )
    except MilvusException as exc:
        logger.error("failed to query paper", arxiv_id=arxiv_id, error=str(exc))
        raise StoreError(f"Failed to get paper arxiv_id={arxiv_id}: {exc}") from exc
    return cast("list[dict[str, Any]]", rows)[0] if rows else None


def arxiv_paper_exists(arxiv_id: str) -> bool:
    """Check whether *arxiv_id* exists."""
    client = get_client()
    safe_id = escape_sql(arxiv_id)
    try:
        rows = client.query(
            "arxiv_papers",
            filter=f"arxiv_id == '{safe_id}'",
            output_fields=["arxiv_id"],
            consistency_level=QUERY_CONSISTENCY,
            limit=1,
        )
    except MilvusException as exc:
        logger.error("failed to check paper existence", arxiv_id=arxiv_id, error=str(exc))
        raise StoreError(f"Failed to check paper arxiv_id={arxiv_id}: {exc}") from exc
    return len(rows) > 0


# ── Chunk ingestion ────────────────────────────────────────────────────────────


def insert_arxiv_chunks(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Idempotently insert chunks — deletes existing chunks first.

    Behaviour depends on the batch composition:

    - **Single-paper batch**: deletes ALL chunks for that paper before inserting the
      new ones.  This is a full replacement — any existing chunks not in `chunks`
      are lost.
    - **Multi-paper batch**: surgically deletes only the specific ``(arxiv_id,
      chunk_idx)`` pairs being inserted, leaving other chunks for the same papers
      untouched.  Use this when you are selectively updating a subset of chunks
      across multiple papers.

    Callers should be aware of this asymmetry — feeding a batch of chunks that
    happens to cover only one paper triggers a full wipe, while a batch covering
    multiple papers does not.
    """
    if not chunks:
        return {"insert_count": 0, "ids": []}
    for c in chunks:
        _validate_chunk_insert(c)
    client = get_client()
    by_paper: dict[str, list[int]] = {}
    for c in chunks:
        by_paper.setdefault(c["arxiv_id"], []).append(c["chunk_idx"])
    try:
        with _WRITE_LOCK:
            if len(by_paper) == 1:
                aid = next(iter(by_paper))
                _delete_chunks_locked(client, aid)
            else:
                for aid, indices in by_paper.items():
                    safe_id = escape_sql(aid)
                    idx_list = ", ".join(str(i) for i in set(indices))
                    flt = f"arxiv_id == '{safe_id}' and chunk_idx in [{idx_list}]"
                    client.delete("arxiv_chunks", filter=flt, consistency_level=DELETE_CONSISTENCY)
            result = client.insert("arxiv_chunks", data=chunks, consistency_level="Strong")
    except MilvusException as exc:
        logger.error("failed to insert arxiv chunks", count=len(chunks), error=str(exc))
        raise StoreError(f"Failed to insert {len(chunks)} chunks: {exc}") from exc
    logger.info("arxiv chunks inserted", count=len(chunks))
    return cast("dict[str, Any]", result)


def upsert_arxiv_chunks(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Upsert chunks — single atomic operation, no OOM risk.

    Unlike ``insert_arxiv_chunks`` (which does delete-before-insert), this uses
    the pymilvus ``upsert`` API: primary-key duplicates are overwritten in-place
    without a separate delete round-trip. This is ideal for high-concurrency
    ingestion where ``delete`` memory quota could be exhausted.

    Each chunk MUST carry ``chunk_id`` as the primary key.
    """
    if not chunks:
        return {"upsert_count": 0, "ids": []}
    for c in chunks:
        _validate_chunk_insert(c)
    client = get_client()
    try:
        with _WRITE_LOCK:
            result = client.upsert("arxiv_chunks", data=chunks)
    except MilvusException as exc:
        logger.error("failed to upsert arxiv chunks", count=len(chunks), error=str(exc))
        raise StoreError(f"Failed to upsert {len(chunks)} chunks: {exc}") from exc
    logger.info("arxiv chunks upserted", count=len(chunks))
    return cast("dict[str, Any]", result)


def delete_arxiv_chunks_by_paper(arxiv_id: str) -> int:
    """Delete all chunks for *arxiv_id*."""
    client = get_client()
    with _WRITE_LOCK:
        count = _delete_chunks_locked(client, arxiv_id)
    logger.info("arxiv chunks deleted by paper", arxiv_id=arxiv_id, count=count)
    return count


def update_arxiv_chunk(chunk_id: str, fields: dict[str, Any]) -> bool:
    """Update only *fields* on *chunk_id* via pymilvus partial_update.

    Uses ``partial_update=True`` (supported on Zilliz Cloud) — only the primary
    key and *fields* are sent.  Vector embeddings and all other columns are left
    untouched.  This is both faster and safer than the old read→merge→upsert
    pattern, because it avoids a race window where a concurrent writer could
    overwrite the fields set by *this* call.

    Returns ``True`` when the upsert succeeds (``upsert_count > 0``) and
    ``False`` when no matching row was found.
    """
    client = get_client()
    data = {"chunk_id": chunk_id, **fields}
    try:
        with _WRITE_LOCK:
            result = client.upsert(
                "arxiv_chunks",
                data=[data],
                partial_update=True,
                consistency_level="Strong",
            )
        upserted = result.get("upsert_count", 0)
        if upserted == 0:
            return False
    except MilvusException as exc:
        logger.error("failed to update chunk", chunk_id=chunk_id, error=str(exc))
        raise StoreError(f"Failed to update chunk chunk_id={chunk_id}: {exc}") from exc
    logger.info("chunk updated", chunk_id=chunk_id)
    return True


def get_arxiv_chunks_by_paper(arxiv_id: str, limit: int = 10000) -> list[dict[str, Any]]:
    client = get_client()
    safe_id = escape_sql(arxiv_id)
    try:
        rows = client.query(
            "arxiv_chunks",
            filter=f"arxiv_id == '{safe_id}'",
            output_fields=list(CHUNK_ALL_FIELDS),
            consistency_level=QUERY_CONSISTENCY,
            limit=limit,
        )
    except MilvusException as exc:
        logger.error("failed to query arxiv chunks", arxiv_id=arxiv_id, error=str(exc))
        raise StoreError(f"Failed to get arxiv chunks for arxiv_id={arxiv_id}: {exc}") from exc
    rows.sort(key=lambda r: r["chunk_idx"])
    return cast("list[dict[str, Any]]", rows)


# ── Pipeline query helpers (flag-based) ───────────────────────────────────────


# Valid resource flags.
_FLAGS = frozenset({"has_latex", "has_pdf", "has_markdown", "has_chunks"})


def query_papers_without(flag: str, batch_size: int = 1000) -> list[dict[str, Any]]:
    """Return papers where *flag* is False.

    Pipe2: ``query_papers_without("has_latex")`` — papers needing LaTeX download.
    Pipe3: ``query_papers_without("has_markdown")`` — papers needing parsing.
    """
    if flag not in _FLAGS:
        raise ValueError(f"Unknown flag: {flag}. Expected one of {sorted(_FLAGS)}")
    client = get_client()
    try:
        rows = client.query(
            "arxiv_papers",
            filter=f"{flag} == False",
            output_fields=["arxiv_id", "created", "updated"],
            consistency_level=QUERY_CONSISTENCY,
            limit=batch_size,
        )
    except MilvusException as exc:
        logger.error("failed to query papers without flag", flag=flag, error=str(exc))
        raise StoreError(f"Failed to query papers without {flag}: {exc}") from exc
    logger.debug("queried papers without flag", flag=flag, count=len(rows))
    return cast("list[dict[str, Any]]", rows)


def count_papers_without(flag: str) -> int:
    """Count papers where *flag* is False (cursor-based, O(n))."""
    if flag not in _FLAGS:
        raise ValueError(f"Unknown flag: {flag}. Expected one of {sorted(_FLAGS)}")
    client = get_client()
    total = 0
    last_id = ""
    while True:
        flt = f"arxiv_id > '{escape_sql(last_id)}'" if last_id else "arxiv_id != ''"
        try:
            rows = client.query(
                "arxiv_papers",
                filter=flt,
                output_fields=["arxiv_id", flag],
                consistency_level=QUERY_CONSISTENCY,
                limit=10000,
            )
        except MilvusException as exc:
            logger.error("failed to count papers without flag", flag=flag, error=str(exc))
            raise StoreError(f"Failed to count papers without {flag}: {exc}") from exc
        if not rows:
            break
        for r in rows:
            if not r.get(flag, False):
                total += 1
        last_id = rows[-1]["arxiv_id"]
    return total
