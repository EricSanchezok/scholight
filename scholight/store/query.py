"""Zilliz Cloud search + query for arxiv_papers and arxiv_chunks — two-phase retrieval on Zilliz Cloud."""

import time
from collections.abc import Generator
from itertools import islice
from typing import Any, cast

import structlog
from pymilvus import AnnSearchRequest, MilvusClient, WeightedRanker

from scholight.config import settings
from scholight.store.client import QUERY_CONSISTENCY, SEARCH_CONSISTENCY, get_client
from scholight.store.fields import (
    CHUNK_SEARCH_FIELDS,
    PAPER_SEARCH_FIELDS,
)

logger = structlog.get_logger(__name__)

# ── Backward-compatible aliases (from fields.py) ──────────────────────────────

PAPER_OUTPUT_FIELDS: list[str] = list(PAPER_SEARCH_FIELDS)
CHUNK_OUTPUT_FIELDS: list[str] = list(CHUNK_SEARCH_FIELDS)

# ── Scale tuning constants ────────────────────────────────────────────────────

MAX_FILTER_IDS: int = 50000
_ARXIV_ID_BATCH_SIZE: int = 500


# ── Filter expression builder ─────────────────────────────────────────────────


def _build_filter(
    *,
    categories: list[str] | None = None,
    authors: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    arxiv_ids: list[str] | None = None,
) -> str:
    """Build a Zilliz Cloud scalar filter expression string.

    For ARRAY fields (categories, authors), uses ``array_contains()``.
    For VARCHAR fields (arxiv_id, created), uses ``in`` or comparison operators.
    """
    parts: list[str] = []

    if categories:
        escaped = " or ".join(f'array_contains(categories, "{_escape_dq(c)}")' for c in categories)
        parts.append(f"({escaped})")

    if authors:
        escaped = " or ".join(f'array_contains(authors, "{_escape_dq(a)}")' for a in authors)
        parts.append(f"({escaped})")

    if date_from is not None:
        parts.append(f'created >= "{date_from}"')

    if date_to is not None:
        parts.append(f'created <= "{date_to}"')

    if arxiv_ids:
        if len(arxiv_ids) > MAX_FILTER_IDS:
            raise ValueError(
                f"arxiv_ids count {len(arxiv_ids)} exceeds MAX_FILTER_IDS ({MAX_FILTER_IDS})"
            )
        escaped = ", ".join(f'"{_escape_dq(aid)}"' for aid in arxiv_ids)
        parts.append(f"(arxiv_id in [{escaped}])")

    return " and ".join(parts) if parts else ""


# ── Internal helpers ──────────────────────────────────────────────────────────


def _escape_dq(val: str) -> str:
    """Escape backslashes and double quotes in Milvus string literals."""
    return val.replace("\\", "\\\\").replace('"', '\\"')


def _batched(iterable: list[Any], n: int) -> Generator[list[Any], None, None]:
    """Yield successive *n*-sized slices from *iterable*."""
    it = iter(iterable)
    while batch := list(islice(it, n)):
        yield batch


def _to_hit(hit: dict[str, Any]) -> dict[str, Any]:
    """Normalise a pymilvus search result dict → ``{score, **entity_fields}``."""
    return {"score": hit["distance"], **hit["entity"]}


def _search_dense(
    *,
    client: MilvusClient,
    collection: str,
    vector: list[float],
    anns_field: str,
    top_k: int,
    filter_expr: str,
    output_fields: list[str],
    level: int,
    timeout: float | None = None,
) -> list[dict[str, Any]]:
    """Run a single dense vector search and return normalised hits."""
    t0 = time.perf_counter()
    results = client.search(
        collection_name=collection,
        data=[vector],
        anns_field=anns_field,
        search_params={"metric_type": "COSINE", "params": {"level": level}},
        limit=top_k,
        filter=filter_expr,
        output_fields=output_fields,
        consistency_level=SEARCH_CONSISTENCY,
        timeout=timeout,
    )
    hits = results[0]
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    logger.debug(
        "dense search",
        collection=collection,
        anns_field=anns_field,
        top_k=top_k,
        hit_count=len(hits),
        filter_expr=filter_expr,
        elapsed_ms=elapsed_ms,
    )
    return [_to_hit(h) for h in hits]


def _search_dense_batched(
    *,
    client: MilvusClient,
    collection: str,
    vector: list[float],
    anns_field: str,
    top_k: int,
    filter_exprs: list[str],
    output_fields: list[str],
    level: int,
    dedup_key: str = "arxiv_id",
    timeout: float | None = None,
) -> list[dict[str, Any]]:
    """Run dense search across multiple filter expressions, deduplicate, merge.

    Used when *arxiv_ids* exceed the batch size — each batch gets its own filter.
    Results are de-duplicated by *dedup_key* and sorted by score descending.
    """
    all_hits: list[dict[str, Any]] = []
    seen: set[str] = set()

    for expr in filter_exprs:
        hits = _search_dense(
            client=client,
            collection=collection,
            vector=vector,
            anns_field=anns_field,
            top_k=top_k,
            filter_expr=expr,
            output_fields=output_fields,
            level=level,
            timeout=timeout,
        )
        for hit in hits:
            key = hit.get(dedup_key)
            if key is not None and key not in seen:
                seen.add(key)
                all_hits.append(hit)

    all_hits.sort(key=lambda h: h["score"], reverse=True)
    return all_hits[:top_k]


# ── Phase 1: Papers search ────────────────────────────────────────────────────


def search_arxiv_papers(
    query_vector: list[float],
    top_k: int = 100,
    *,
    categories: list[str] | None = None,
    authors: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    arxiv_ids: list[str] | None = None,
    output_fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Phase 1: Dense vector search on arxiv_papers with scalar filters.

    Returns list of dicts with entity fields + ``score`` field,
    sorted by score descending (COSINE distance → higher is better).
    """
    client = get_client()
    fields = output_fields if output_fields is not None else list(PAPER_SEARCH_FIELDS)

    logger.debug(
        "search_arxiv_papers",
        top_k=top_k,
        categories=categories,
        authors=authors,
        date_from=date_from,
        date_to=date_to,
    )

    # Build base filter expression (everything except arxiv_ids)
    base_expr = _build_filter(
        categories=categories,
        authors=authors,
        date_from=date_from,
        date_to=date_to,
    )

    if arxiv_ids and len(arxiv_ids) > _ARXIV_ID_BATCH_SIZE:
        # Batch arxiv_ids to avoid oversized filter expressions
        filter_exprs: list[str] = []
        for batch in _batched(arxiv_ids, _ARXIV_ID_BATCH_SIZE):
            ids_expr = _build_filter(arxiv_ids=batch)
            filter_exprs.append(f"({base_expr}) and ({ids_expr})" if base_expr else ids_expr)
        return _search_dense_batched(
            client=client,
            collection="arxiv_papers",
            vector=query_vector,
            anns_field="abstract_embedding",
            top_k=top_k,
            filter_exprs=filter_exprs,
            output_fields=fields,
            level=settings.search_level,
        )

    filter_expr = _build_filter(
        categories=categories,
        authors=authors,
        date_from=date_from,
        date_to=date_to,
        arxiv_ids=arxiv_ids,
    )
    return _search_dense(
        client=client,
        collection="arxiv_papers",
        vector=query_vector,
        anns_field="abstract_embedding",
        top_k=top_k,
        filter_expr=filter_expr,
        output_fields=fields,
        level=settings.search_level,
    )


def hybrid_search_arxiv_papers(
    query_vector: list[float],
    query_text: str,
    top_k: int = 100,
    *,
    categories: list[str] | None = None,
    authors: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    arxiv_ids: list[str] | None = None,  # noqa: ARG001 — reserved for future wiring
    output_fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Phase 1 hybrid: Dense + BM25 two-way combined search on arxiv_papers.

    Two-way fusion:
      - ``abstract_embedding`` (COSINE dense) — semantic recall
      - ``abstract_bm25`` (BM25 Function) — keyword recall on abstracts

    Uses ``WeightedRanker`` with weights from
    ``SCHOLIGHT_SEARCH_HYBRID_DENSE_WEIGHT`` and ``SCHOLIGHT_SEARCH_HYBRID_BM25_WEIGHT``.
    """
    client = get_client()
    fields = output_fields if output_fields is not None else list(PAPER_SEARCH_FIELDS)
    filter_expr = _build_filter(
        categories=categories,
        authors=authors,
        date_from=date_from,
        date_to=date_to,
    )

    logger.debug(
        "hybrid_search_arxiv_papers",
        top_k=top_k,
        filter_expr=filter_expr,
    )

    dense_req = AnnSearchRequest(
        data=[query_vector],
        anns_field="abstract_embedding",
        param={"metric_type": "COSINE", "params": {"level": settings.search_level}},
        limit=top_k,
        filter=filter_expr or None,
    )
    bm25_req = AnnSearchRequest(
        data=[query_text],
        anns_field="abstract_bm25",
        param={"metric_type": "BM25"},
        limit=top_k,
        filter=filter_expr or None,
    )

    ranker = WeightedRanker(
        settings.search_hybrid_dense_weight,
        settings.search_hybrid_bm25_weight,
    )
    logger.debug(
        "hybrid search ranker",
        strategy="weighted",
        weights=[settings.search_hybrid_dense_weight, settings.search_hybrid_bm25_weight],
    )

    t0 = time.perf_counter()
    results = client.hybrid_search(
        collection_name="arxiv_papers",
        reqs=[dense_req, bm25_req],
        ranker=ranker,
        limit=top_k,
        output_fields=fields,
        consistency_level=SEARCH_CONSISTENCY,
    )
    hits = [_to_hit(h) for h in results[0]]
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    logger.debug(
        "hybrid papers search done",
        top_k=top_k,
        hit_count=len(hits),
        elapsed_ms=elapsed_ms,
    )
    return hits


# ── Phase 2: Chunks search ────────────────────────────────────────────────────


def search_arxiv_chunks(
    query_vector: list[float],
    arxiv_ids: list[str],
    top_k: int = 20,
    *,
    output_fields: list[str] | None = None,
    timeout: float | None = None,
) -> list[dict[str, Any]]:
    """Phase 2: Dense vector search on arxiv_chunks, filtered to given *arxiv_ids*."""
    client = get_client()
    fields = output_fields if output_fields is not None else list(CHUNK_SEARCH_FIELDS)

    if not arxiv_ids:
        return []

    logger.debug("search_arxiv_chunks", top_k=top_k, n_arxiv_ids=len(arxiv_ids))

    if len(arxiv_ids) > _ARXIV_ID_BATCH_SIZE:
        filter_exprs = [
            _build_filter(arxiv_ids=batch) for batch in _batched(arxiv_ids, _ARXIV_ID_BATCH_SIZE)
        ]
        return _search_dense_batched(
            client=client,
            collection="arxiv_chunks",
            vector=query_vector,
            anns_field="content_embedding",
            top_k=top_k,
            filter_exprs=filter_exprs,
            output_fields=fields,
            level=settings.chunk_search_level,
            dedup_key="chunk_id",
            timeout=timeout,
        )

    filter_expr = _build_filter(arxiv_ids=arxiv_ids)
    return _search_dense(
        client=client,
        collection="arxiv_chunks",
        vector=query_vector,
        anns_field="content_embedding",
        top_k=top_k,
        filter_expr=filter_expr,
        output_fields=fields,
        level=settings.chunk_search_level,
        timeout=timeout,
    )


def hybrid_search_arxiv_chunks(
    query_vector: list[float],
    arxiv_ids: list[str],
    query_text: str,
    top_k: int = 20,
    *,
    output_fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Phase 2 hybrid: Dense + BM25 two-way search on arxiv_chunks, filtered to *arxiv_ids*."""
    client = get_client()
    fields = output_fields if output_fields is not None else list(CHUNK_SEARCH_FIELDS)

    if not arxiv_ids:
        return []

    logger.debug(
        "hybrid_search_arxiv_chunks",
        top_k=top_k,
        n_arxiv_ids=len(arxiv_ids),
    )

    # Build filter batches
    batches = list(_batched(arxiv_ids, _ARXIV_ID_BATCH_SIZE))
    all_hits: list[dict[str, Any]] = []
    seen: set[str] = set()

    for batch in batches:
        filter_expr = _build_filter(arxiv_ids=batch)
        dense_req = AnnSearchRequest(
            data=[query_vector],
            anns_field="content_embedding",
            param={"metric_type": "COSINE", "params": {"level": settings.chunk_search_level}},
            limit=top_k,
            filter=filter_expr,
        )
        bm25_req = AnnSearchRequest(
            data=[query_text],
            anns_field="content_bm25",
            param={"metric_type": "BM25"},
            limit=top_k,
            filter=filter_expr,
        )
        t0 = time.perf_counter()
        results = client.hybrid_search(
            collection_name="arxiv_chunks",
            reqs=[dense_req, bm25_req],
            ranker=WeightedRanker(
                settings.search_hybrid_dense_weight,
                settings.search_hybrid_bm25_weight,
            ),
            limit=top_k,
            output_fields=fields,
            consistency_level=SEARCH_CONSISTENCY,
        )
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        for hit in results[0]:
            h = _to_hit(hit)
            cid = h.get("chunk_id")
            if cid is not None and cid not in seen:
                seen.add(cid)
                all_hits.append(h)
        logger.debug(
            "hybrid chunks batch done",
            batch_size=len(batch),
            batch_hits=len(results[0]),
            elapsed_ms=elapsed_ms,
        )

    all_hits.sort(key=lambda h: h["score"], reverse=True)
    return all_hits[:top_k]


# ── Full-collection chunks search (no arxiv_id filter) ────────────────────────


def search_all_chunks(
    query_vector: list[float],
    top_k: int = 20,
    *,
    output_fields: list[str] | None = None,
    level: int | None = None,
) -> list[dict[str, Any]]:
    """Dense vector search across ALL arxiv_chunks — no arxiv_id filter.

    Designed for full-collection retrieval at scale.
    Uses ``settings.chunk_search_level`` by default; pass *level* to override per-call.

    Returns list of dicts with entity fields + ``score`` field,
    sorted by score descending (COSINE distance → higher is better).
    """
    client = get_client()
    fields = output_fields if output_fields is not None else list(CHUNK_SEARCH_FIELDS)
    _level = level if level is not None else settings.chunk_search_level

    logger.debug("search_all_chunks", top_k=top_k, level=_level)
    return _search_dense(
        client=client,
        collection="arxiv_chunks",
        vector=query_vector,
        anns_field="content_embedding",
        top_k=top_k,
        filter_expr="",
        output_fields=fields,
        level=_level,
    )


def hybrid_search_all_chunks(
    query_vector: list[float],
    query_text: str,
    top_k: int = 20,
    *,
    output_fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Hybrid (Dense + BM25) search across ALL arxiv_chunks — no arxiv_id filter.

    Uses ``settings.chunk_search_level`` for the dense leg.
    """
    client = get_client()
    fields = output_fields if output_fields is not None else list(CHUNK_SEARCH_FIELDS)

    logger.debug(
        "hybrid_search_all_chunks",
        top_k=top_k,
    )

    dense_req = AnnSearchRequest(
        data=[query_vector],
        anns_field="content_embedding",
        param={"metric_type": "COSINE", "params": {"level": settings.chunk_search_level}},
        limit=top_k,
    )
    bm25_req = AnnSearchRequest(
        data=[query_text],
        anns_field="content_bm25",
        param={"metric_type": "BM25"},
        limit=top_k,
    )

    t0 = time.perf_counter()
    results = client.hybrid_search(
        collection_name="arxiv_chunks",
        reqs=[dense_req, bm25_req],
        ranker=WeightedRanker(
            settings.search_hybrid_dense_weight,
            settings.search_hybrid_bm25_weight,
        ),
        limit=top_k,
        filter="",
        output_fields=fields,
        consistency_level=SEARCH_CONSISTENCY,
    )
    hits = [_to_hit(h) for h in results[0]]
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    logger.debug(
        "hybrid all chunks search done",
        top_k=top_k,
        hit_count=len(hits),
        elapsed_ms=elapsed_ms,
    )
    return hits


def bm25_search_all_chunks(
    query_text: str,
    top_k: int = 1024,
    *,
    output_fields: list[str] | None = None,
    timeout: float | None = 120,
) -> list[dict[str, Any]]:
    """BM25 sparse search across ALL arxiv_chunks via Zilliz BM25 Function.

    Returns chunk hits with entity fields + ``score`` field,
    sorted by score descending (higher is better).

    ~50 ms — inverted-index lookup, independent of collection size.
    """
    client = get_client()
    fields = output_fields if output_fields is not None else list(CHUNK_SEARCH_FIELDS)
    t0 = time.perf_counter()
    results = client.search(
        collection_name="arxiv_chunks",
        data=[query_text],
        anns_field="content_bm25",
        search_params={"metric_type": "BM25"},
        limit=top_k,
        output_fields=fields,
        consistency_level=SEARCH_CONSISTENCY,
        timeout=timeout,
    )
    hits = [_to_hit(h) for h in results[0]]
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    logger.debug(
        "bm25 all chunks search done",
        top_k=top_k,
        hit_count=len(hits),
        elapsed_ms=elapsed_ms,
    )
    return hits


def batch_get_arxiv_papers(
    arxiv_ids: list[str],
    *,
    categories: list[str] | None = None,
    authors: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    output_fields: list[str] | None = None,
    timeout: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Batch-fetch paper metadata for candidate *arxiv_ids*.

    Uses ``client.query()`` with the same paper-level scalar filters as public
    search, batching 500 ids per call (``_ARXIV_ID_BATCH_SIZE``). Returns a
    dict keyed by arxiv_id; missing or ineligible papers are absent.
    """
    if not arxiv_ids:
        return {}
    client = get_client()
    fields = output_fields if output_fields is not None else list(PAPER_SEARCH_FIELDS)
    results: dict[str, dict[str, Any]] = {}
    for batch in _batched(arxiv_ids, _ARXIV_ID_BATCH_SIZE):
        expr = _build_filter(
            categories=categories,
            authors=authors,
            date_from=date_from,
            date_to=date_to,
            arxiv_ids=batch,
        )
        rows = cast(
            "list[dict[str, Any]]",
            client.query(
                collection_name="arxiv_papers",
                filter=expr,
                output_fields=fields,
                consistency_level=QUERY_CONSISTENCY,
                timeout=timeout,
            ),
        )
        for row in rows:
            results[row["arxiv_id"]] = row
    return results


# ── Exact lookups ─────────────────────────────────────────────────────────────


def get_arxiv_paper_by_id(
    arxiv_id: str,
    output_fields: list[str] | None = None,
) -> dict[str, Any] | None:
    """Exact lookup by *arxiv_id*. Uses Strong consistency for correctness."""
    client = get_client()
    fields = output_fields if output_fields is not None else list(PAPER_SEARCH_FIELDS)

    escaped_id = _escape_dq(arxiv_id)
    results = client.query(
        collection_name="arxiv_papers",
        filter=f'arxiv_id == "{escaped_id}"',
        output_fields=fields,
        consistency_level=QUERY_CONSISTENCY,
    )
    return cast("list[dict[str, Any]]", results)[0] if results else None
