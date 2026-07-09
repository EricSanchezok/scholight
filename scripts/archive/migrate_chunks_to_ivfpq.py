#!/usr/bin/env python3
"""Migrate arxiv_chunks.content_embedding from HNSW to IVF_PQ.

Index build time: 8-14h for 79M x 1024-dim vectors on CPU.
Dense search on arxiv_chunks will be UNAVAILABLE during migration.
BM25 sparse search and all arxiv_papers searches remain functional.

Usage:
    python scripts/migrate_chunks_to_ivfpq.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import structlog
from pymilvus.milvus_client.index import IndexParams
from pymilvus.orm.utility import index_building_progress

_project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_project_root))

from scholight.config import settings  # noqa: E402
from scholight.logging import configure_logging  # noqa: E402
from scholight.storage import storage  # noqa: E402
from scholight.store.client import get_client  # noqa: E402

_LOG_FILE = storage.log_path("migrate_ivfpq", "migrate.log")
configure_logging(
    log_level=settings.log_level,
    use_json=True,
    file_handler=(str(_LOG_FILE), 50_000_000, 3),
)
logger = structlog.get_logger("migrate-ivfpq")

INDEX_NAME: str = "idx_arxiv_chunks_content_dense"
COLLECTION: str = "arxiv_chunks"
IVF_PQ_PARAMS: dict[str, int] = {"nlist": 10000, "m": 64, "nbits": 8}
_LOAD_TIMEOUT: int = 3600
_POLL_TIMEOUT: int = 86400  # 24h
_POLL_INTERVAL: int = 60


def _phase(header: str) -> None:
    """Log a phase header."""
    sep = "=" * 60
    logger.info(sep)
    logger.info("PHASE: %s", header)
    logger.info(sep)


def _confirm(msg: str = "Continue? [y/N] ") -> bool:
    """Prompt for user confirmation."""
    response = input(msg).strip().lower()
    return response in ("y", "yes")


def _verify(client: Any) -> None:
    """Phase 0: Verify collection exists, HNSW index present, log row count."""
    if COLLECTION not in client.list_collections():
        logger.error("collection does not exist", collection=COLLECTION)
        sys.exit(1)

    indexes: list[str] = client.list_indexes(COLLECTION)
    logger.info("existing indexes", collection=COLLECTION, indexes=indexes)

    if INDEX_NAME not in indexes:
        logger.error("target index not found", index_name=INDEX_NAME, collection=COLLECTION)
        sys.exit(1)

    stats: dict[str, Any] = client.get_collection_stats(COLLECTION)
    logger.info(
        "collection stats",
        collection=COLLECTION,
        row_count=stats.get("row_count", "unknown"),
    )

    logger.warning(
        "\n"
        "  ⚠  Dense search on arxiv_chunks will be UNAVAILABLE for 8-14 hours.\n"
        "  ✓  BM25 sparse search and all arxiv_papers searches remain functional."
    )
    auto_yes = "--yes" in sys.argv
    if not auto_yes and not _confirm("Proceed with migration? [y/N] "):
        logger.info("aborted by user")
        sys.exit(0)
    logger.info("confirmation %s", "auto-yes" if auto_yes else "manual")


def _drop_old_index(client: Any) -> None:
    """Phase 1: Release collection, then drop old HNSW index."""
    client.release_collection(COLLECTION)
    logger.info("collection released", collection=COLLECTION)

    client.drop_index(COLLECTION, INDEX_NAME)
    logger.info("old HNSW index dropped", index_name=INDEX_NAME)


def _create_ivfpq_index(client: Any) -> None:
    """Phase 2: Create IVF_PQ index on content_embedding."""
    ip = IndexParams()
    ip.add_index(
        field_name="content_embedding",
        index_type="IVF_PQ",
        index_name=INDEX_NAME,
        metric_type="COSINE",
        **IVF_PQ_PARAMS,
    )
    client.create_index(COLLECTION, index_params=ip)
    logger.info(
        "IVF_PQ index build submitted — expected 8-14h, polling every %ds",
        _POLL_INTERVAL,
        nlist=IVF_PQ_PARAMS["nlist"],
        m=IVF_PQ_PARAMS["m"],
        nbits=IVF_PQ_PARAMS["nbits"],
    )


def _poll_until_complete(client: Any) -> None:
    """Phase 3: Poll describe_index every 60s until Finished or 24h timeout."""
    deadline = time.monotonic() + _POLL_TIMEOUT
    last_pct: float = -1.0
    state: str = "Unknown"

    while time.monotonic() < deadline:
        info: dict[str, Any] = client.describe_index(COLLECTION, INDEX_NAME)
        state = info.get("state", "Unknown")

        try:
            progress = index_building_progress(COLLECTION, INDEX_NAME)
            total = progress.get("total_rows", 0)
            indexed = progress.get("indexed_rows", 0)
            if total > 0:
                pct = round(indexed / total * 100, 1)
                if pct != last_pct:
                    logger.info(
                        "building index",
                        progress=f"{pct}%",
                        indexed_rows=indexed,
                        total_rows=total,
                        state=state,
                    )
                    last_pct = pct
        except Exception:
            logger.debug("index building progress unavailable", state=state)

        if state == "Finished":
            logger.info("index build complete", index_name=INDEX_NAME)
            return

        logger.debug("waiting for index", index=INDEX_NAME, state=state)
        time.sleep(_POLL_INTERVAL)

    logger.error(
        "index build timed out after 24h",
        index_name=INDEX_NAME,
        last_state=state,
    )
    sys.exit(1)


def _validate_search(client: Any) -> None:
    """Phase 4: Load collection and run 3 sample dense searches with nprobe=32."""
    client.load_collection(COLLECTION, timeout=_LOAD_TIMEOUT)
    logger.info("collection loaded", collection=COLLECTION)

    nprobe: int = int(settings.chunk_search_nprobe)
    search_params: dict[str, Any] = {
        "metric_type": "COSINE",
        "params": {"nprobe": nprobe},
    }

    # Unit vector: each component = 1/sqrt(dim) so ||v|| = 1.
    dim: int = settings.embedding_dim
    component: float = 1.0 / (dim**0.5)
    query_vector: list[float] = [component] * dim

    for i in range(3):
        t0 = time.perf_counter()
        results = client.search(
            collection_name=COLLECTION,
            data=[query_vector],
            anns_field="content_embedding",
            search_params=search_params,
            limit=10,
            output_fields=["chunk_id", "arxiv_id"],
        )
        hits = results[0]
        elapsed = round((time.perf_counter() - t0) * 1000, 1)
        top_scores = [round(h["distance"], 4) for h in hits[:3]]
        logger.info(
            "validation search",
            iteration=i + 1,
            hits=len(hits),
            elapsed_ms=elapsed,
            top_scores=top_scores,
            nprobe=nprobe,
        )
        if not hits:
            logger.warning("validation search returned no hits — index may be incomplete")
            return


def run() -> None:
    """Execute the full HNSW → IVF_PQ migration with phased confirmation."""
    client = get_client()

    _phase("0: Verify")
    _verify(client)

    _phase("1: Release + drop old HNSW index")
    _drop_old_index(client)

    _phase("2: Create IVF_PQ index")
    _create_ivfpq_index(client)

    _phase("3: Poll for index build completion")
    _poll_until_complete(client)

    _phase("4: Load + validate searches")
    _validate_search(client)

    logger.info(
        "✅ Migration complete — arxiv_chunks.content_embedding now uses IVF_PQ",
        index_name=INDEX_NAME,
        nlist=IVF_PQ_PARAMS["nlist"],
        m=IVF_PQ_PARAMS["m"],
        nbits=IVF_PQ_PARAMS["nbits"],
    )


if __name__ == "__main__":
    run()
