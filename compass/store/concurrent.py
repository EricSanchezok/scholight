"""Concurrent bulk ingestion for arxiv_papers and arxiv_chunks.

Each worker thread creates its own :class:`pymilvus.MilvusClient` instance
(not the global singleton) and the dispatcher shards data by arxiv_id range
for chunks (avoiding cross-worker overlap) and by index for papers (where PK
is already guaranteed unique by the caller).  No global ``_WRITE_LOCK`` needed.

.. note::

   The *arxiv_papers* sharding is by positional slice.  Callers that pass
   papers spanning the same ``arxiv_id`` across workers may see the last
   upsert win (Zilliz Cloud PK overwrite).  This is intentional — order matters,
   and the dispatcher preserves input order within each shard.

   The *arxiv_chunks* sharding groups by arxiv_id so each paper's chunks are
   assigned to exactly one worker.  This avoids the race condition where two
   workers delete-then-insert chunks for the same paper, silently losing data.
"""

from __future__ import annotations

import concurrent.futures
import os
from collections.abc import Callable
from typing import Any

import structlog
from pymilvus import MilvusClient
from pymilvus.exceptions import MilvusException

from compass.store.client import batched, escape_sql
from compass.store.ingest import (
    StoreError,
    _validate_paper_full_upsert,
)

logger = structlog.get_logger(__name__)

# ── Defaults ────────────────────────────────────────────────────────────

DEFAULT_CONCURRENCY: int = min(8, (os.cpu_count() or 4))


# ── Client factory ──────────────────────────────────────────────────────

_STRICT_TIMEOUT: int = 30  # Match CONNECT_TIMEOUT in client.py


def _worker_client() -> MilvusClient:
    """Create a dedicated MilvusClient for a worker thread."""
    from compass.store.client import _resolve_token, _resolve_uri

    return MilvusClient(uri=_resolve_uri(), token=_resolve_token(), timeout=_STRICT_TIMEOUT)


# ── Per-worker jobs ─────────────────────────────────────────────────────


def _worker_insert_papers(papers: list[dict[str, Any]]) -> dict[str, Any]:
    """Insert/upsert a batch of arxiv papers in a worker thread.

    Uses full ``client.upsert`` — each paper dict MUST include all schema
    fields, especially vector fields.  ``_validate_paper_full_upsert`` guards
    against missing or empty fields at write time.
    """
    client = _worker_client()
    inserted = 0
    try:
        for batch in batched(papers):
            for p in batch:
                _validate_paper_full_upsert(p)
            result = client.upsert("arxiv_papers", data=batch, consistency_level="Strong")
            inserted += result.get("upsert_count", len(batch))
    except MilvusException as exc:
        logger.error("worker paper upsert failed", arxiv_count=len(papers), error=str(exc))
        raise StoreError(f"Worker paper upsert failed: {exc}") from exc
    finally:
        client.close()
    return {"inserted": inserted, "updated": 0}


def _worker_insert_chunks(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Insert arxiv chunks in a worker thread.

    Deletes existing chunks for the papers in this batch via a single
    ``arxiv_id IN [...]`` filter before inserting.  This avoids the per-paper
    delete-loop anti-pattern and reduces round-trips.
    """
    client = _worker_client()
    inserted = 0
    try:
        for batch in batched(chunks):
            paper_ids = list({c["arxiv_id"] for c in batch})
            if paper_ids:
                id_list = ", ".join(f"'{escape_sql(aid)}'" for aid in paper_ids)
                client.delete(
                    "arxiv_chunks",
                    filter=f"arxiv_id in [{id_list}]",
                    consistency_level="Strong",
                )
                logger.debug(
                    "worker deleted chunks before insert",
                    paper_count=len(paper_ids),
                    batch_size=len(batch),
                )
            client.insert("arxiv_chunks", data=batch, consistency_level="Strong")
            inserted += len(batch)
    except MilvusException as exc:
        logger.error("worker chunk insert failed", chunk_count=len(chunks), error=str(exc))
        raise StoreError(f"Worker chunk insert failed: {exc}") from exc
    finally:
        client.close()
    return {"inserted": inserted}


# ── Dispatcher ──────────────────────────────────────────────────────────


def _run_workers(
    items: list[Any],
    worker_fn: Callable[[list[Any]], dict[str, Any]],
    concurrency: int,
    label: str,
) -> dict[str, Any]:
    """Shard *items* across *concurrency* workers, gather results."""
    if not items:
        return {"inserted": 0, "updated": 0, "workers": 0}

    n = min(concurrency, max(1, len(items) // 100))
    shard_size = max(1, len(items) // n)
    shards = [items[i : i + shard_size] for i in range(0, len(items), shard_size)]

    logger.info("concurrent insert start", label=label, workers=len(shards), total=len(items))

    total_inserted = 0
    total_updated = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(shards)) as pool:
        futures = {pool.submit(worker_fn, shard): i for i, shard in enumerate(shards)}
        for fut in concurrent.futures.as_completed(futures):
            try:
                result = fut.result()
                total_inserted += result.get("inserted", 0)
                total_updated += result.get("updated", 0)
            except StoreError as exc:
                logger.error("worker failed", label=label, error=str(exc))
                # Cancel remaining futures
                for f in futures:
                    f.cancel()
                raise StoreError(f"Concurrent {label} insert aborted: {exc}") from exc

    logger.info(
        "concurrent insert done",
        label=label,
        inserted=total_inserted,
        updated=total_updated,
        workers=len(shards),
    )
    return {"inserted": total_inserted, "updated": total_updated, "workers": len(shards)}


# ── Public API ──────────────────────────────────────────────────────────


def insert_arxiv_papers_concurrent(
    papers: list[dict[str, Any]], concurrency: int = DEFAULT_CONCURRENCY
) -> dict[str, Any]:
    """Insert arxiv papers in parallel across multiple MilvusClient instances.

    Papers are sharded by index range — each worker handles a disjoint
    subset.  Returns ``{"inserted": N, "updated": N, "workers": N}``.
    """
    return _run_workers(papers, _worker_insert_papers, concurrency, "arxiv_papers")


def insert_arxiv_chunks_concurrent(
    chunks: list[dict[str, Any]], concurrency: int = DEFAULT_CONCURRENCY
) -> dict[str, Any]:
    """Insert arxiv chunks in parallel across multiple MilvusClient instances.

    Each worker deletes-then-inserts for the arxiv papers in its shard.
    Returns ``{"inserted": N, "workers": N}``.
    """
    return _run_workers(chunks, _worker_insert_chunks, concurrency, "arxiv_chunks")
