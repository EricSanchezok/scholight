"""Zilliz Cloud collection DDL for arxiv_papers and arxiv_chunks.

Design decisions for Zilliz Cloud Serverless
=============================================

- All indexes use ``AUTOINDEX`` — Zilliz Cloud automatically selects the
  optimal index type (HNSW for dense, BITMAP/INVERTED/STL_SORT for scalars,
  sparse-inverted for SPARSE_FLOAT_VECTOR).

- Dense vector fields use ``metric_type="COSINE"`` for semantic search.

- BM25 Functions (``FunctionType.BM25``) map analysed text fields to
  ``SPARSE_FLOAT_VECTOR`` outputs.  Sparse index uses ``metric_type="BM25"``.
  Insert/upsert automatically populates the sparse field; search accepts raw
  text as the query and the Function converts it transparently.

- ``shards_num=2`` for both collections (Zilliz Serverless maximum).

- ``consistency_level``: ``"Bounded"`` for papers (meta-data reads can
  tolerate slight staleness), ``"Eventually"`` for chunks (chunk search is
  a ranking signal, not an OLTP guarantee).

- No Partition Key — the natural candidate ``categories`` is an ARRAY type,
  and Zilliz Cloud only supports VARCHAR/INT64 Partition Keys.  Scalar
  AUTOINDEX on ``categories`` provides sufficient filter performance.
"""

import time
from typing import Final

import structlog
from pymilvus import (
    CollectionSchema,
    DataType,
    FieldSchema,
    Function,
    FunctionType,
    MilvusClient,
)
from pymilvus.milvus_client import IndexParams

from scholight.config import settings

logger = structlog.get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

COLLECTION_NAMES: Final[tuple[str, ...]] = ("arxiv_papers", "arxiv_chunks")

_INDEX_POLL_INTERVAL: Final = 5
_INDEX_POLL_TIMEOUT: Final = 86400
_COLLECTION_LOAD_TIMEOUT: Final = 3600

# ── Papers Schema (20 fields) ─────────────────────────────────────────────────

ARXIV_PAPERS_SCHEMA: Final[CollectionSchema] = CollectionSchema(
    fields=[
        FieldSchema(name="arxiv_id", dtype=DataType.VARCHAR, max_length=32, is_primary=True),
        FieldSchema(name="title", dtype=DataType.VARCHAR, max_length=2048),
        FieldSchema(
            name="authors",
            dtype=DataType.ARRAY,
            element_type=DataType.VARCHAR,
            max_length=256,
            max_capacity=4096,
        ),
        FieldSchema(
            name="abstract",
            dtype=DataType.VARCHAR,
            max_length=16384,
            enable_analyzer=True,
        ),
        FieldSchema(
            name="categories",
            dtype=DataType.ARRAY,
            element_type=DataType.VARCHAR,
            max_length=64,
            max_capacity=30,
        ),
        FieldSchema(name="created", dtype=DataType.VARCHAR, max_length=16),
        FieldSchema(name="updated", dtype=DataType.VARCHAR, max_length=16),
        FieldSchema(name="version", dtype=DataType.INT64),
        FieldSchema(
            name="updated_history",
            dtype=DataType.ARRAY,
            element_type=DataType.VARCHAR,
            max_length=32,
            max_capacity=100,
        ),
        FieldSchema(name="license", dtype=DataType.VARCHAR, max_length=512),
        FieldSchema(name="comments", dtype=DataType.VARCHAR, max_length=8192),
        FieldSchema(name="doi", dtype=DataType.VARCHAR, max_length=256),
        FieldSchema(name="journal_ref", dtype=DataType.VARCHAR, max_length=2048),
        FieldSchema(name="acm_class", dtype=DataType.VARCHAR, max_length=256),
        FieldSchema(name="has_latex", dtype=DataType.BOOL),
        FieldSchema(name="has_pdf", dtype=DataType.BOOL),
        FieldSchema(name="has_markdown", dtype=DataType.BOOL),
        FieldSchema(name="has_chunks", dtype=DataType.BOOL),
        FieldSchema(
            name="abstract_embedding",
            dtype=DataType.FLOAT_VECTOR,
            dim=settings.embedding_dim,
        ),
        FieldSchema(name="abstract_bm25", dtype=DataType.SPARSE_FLOAT_VECTOR),
    ],
    description="arXiv paper metadata (20 fields) — dense + BM25 embeddings, resource flags",
)

_PAPERS_BM25_FUNC: Final = Function(
    name="abstract_bm25_func",
    input_field_names=["abstract"],
    output_field_names=["abstract_bm25"],
    function_type=FunctionType.BM25,
)

# ── Chunks Schema (6 fields) ──────────────────────────────────────────────────

ARXIV_CHUNKS_SCHEMA: Final[CollectionSchema] = CollectionSchema(
    fields=[
        FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, max_length=64, is_primary=True),
        FieldSchema(name="arxiv_id", dtype=DataType.VARCHAR, max_length=32),
        FieldSchema(name="chunk_idx", dtype=DataType.INT16),
        FieldSchema(
            name="content_text",
            dtype=DataType.VARCHAR,
            max_length=16384,
            enable_analyzer=True,
        ),
        FieldSchema(
            name="content_embedding",
            dtype=DataType.FLOAT_VECTOR,
            dim=settings.embedding_dim,
        ),
        FieldSchema(name="content_bm25", dtype=DataType.SPARSE_FLOAT_VECTOR),
    ],
    description="Document chunks (6 fields) — chunk_idx for position weighting & evidence",
)

_CHUNKS_BM25_FUNC: Final = Function(
    name="content_bm25_func",
    input_field_names=["content_text"],
    output_field_names=["content_bm25"],
    function_type=FunctionType.BM25,
)


# ── Index builders ────────────────────────────────────────────────────────────


def _build_arxiv_papers_indexes() -> IndexParams:
    """Minimal AUTOINDEX set for arxiv_papers.

    Scalar indexes only on fields that are actually used in filter expressions.
    (See ``_build_filter()`` in `query.py` and daemon ``client.query()`` calls.)
    """
    params = IndexParams()

    # Scalar indexes — only filter fields.
    for field_name in ("authors", "categories", "created"):
        params.add_index(
            field_name=field_name,
            index_type="AUTOINDEX",
            index_name=f"idx_arxiv_papers_{field_name}",
        )
    for flag in ("has_latex", "has_pdf", "has_markdown", "has_chunks"):
        params.add_index(
            field_name=flag,
            index_type="AUTOINDEX",
            index_name=f"idx_arxiv_papers_{flag}",
        )

    # Dense vector (Qwen3-Embedding-0.6B, 1024-dim).
    params.add_index(
        field_name="abstract_embedding",
        index_type="AUTOINDEX",
        index_name="idx_arxiv_papers_abstract_dense",
        metric_type="COSINE",
    )

    # BM25 sparse vector (Zilliz built-in Function).
    params.add_index(
        field_name="abstract_bm25",
        index_type="AUTOINDEX",
        index_name="idx_arxiv_papers_abstract_bm25",
        metric_type="BM25",
    )

    return params


def _build_arxiv_chunks_indexes() -> IndexParams:
    """Minimal AUTOINDEX set for arxiv_chunks (70M+ rows).

    Only ``arxiv_id`` gets a scalar index — it is the sole filter field for
    chunk queries.  ``chunk_idx`` is present in the schema for write-path
    deletes and position weighting; a scalar index is not necessary since
    ``arxiv_id`` narrows the result set to a few dozen rows.
    """
    params = IndexParams()

    # Scalar index — only arxiv_id.
    params.add_index(
        field_name="arxiv_id",
        index_type="AUTOINDEX",
        index_name="idx_arxiv_chunks_arxiv_id",
    )

    # Dense vector.
    params.add_index(
        field_name="content_embedding",
        index_type="AUTOINDEX",
        index_name="idx_arxiv_chunks_content_dense",
        metric_type="COSINE",
    )

    # BM25 sparse vector (Zilliz built-in Function).
    params.add_index(
        field_name="content_bm25",
        index_type="AUTOINDEX",
        index_name="idx_arxiv_chunks_content_bm25",
        metric_type="BM25",
    )

    return params


# ── Helpers ───────────────────────────────────────────────────────────────────


def _wait_for_index(client: MilvusClient, collection: str, index_name: str) -> None:
    """Poll until *index_name* on *collection* reaches ``"Finished"`` state."""
    deadline = time.monotonic() + _INDEX_POLL_TIMEOUT
    while time.monotonic() < deadline:
        info = client.describe_index(collection, index_name)
        if info.get("state") == "Finished":
            return
        logger.debug(
            "waiting for index",
            collection=collection,
            index=index_name,
            state=info.get("state", ""),
        )
        time.sleep(_INDEX_POLL_INTERVAL)
    raise TimeoutError(
        f"Index {index_name!r} on collection {collection!r} did not finish "
        f"within {_INDEX_POLL_TIMEOUT}s"
    )


# ── Public API ────────────────────────────────────────────────────────────────


def create_collections(client: MilvusClient) -> None:
    """Idempotently create arxiv_papers and arxiv_chunks collections.

    Attaches a BM25 :class:`Function` to each collection so Zilliz Cloud
    automatically populates the ``*_bm25`` output fields on insert/upsert.

    Collections that already exist with indexes are left untouched.  A bare
    collection with no indexes is dropped and recreated (indexes are required
    for search).
    """
    _schema_map = {
        "arxiv_papers": ARXIV_PAPERS_SCHEMA,
        "arxiv_chunks": ARXIV_CHUNKS_SCHEMA,
    }
    _bm25_funcs: dict[str, Function] = {
        "arxiv_papers": _PAPERS_BM25_FUNC,
        "arxiv_chunks": _CHUNKS_BM25_FUNC,
    }
    _consistency: dict[str, str] = {
        "arxiv_papers": "Bounded",
        "arxiv_chunks": "Eventually",
    }

    for name in COLLECTION_NAMES:
        if client.has_collection(name):
            indexes = client.list_indexes(name)
            if not indexes:
                logger.warning(
                    "collection exists but has no indexes — recreating",
                    collection=name,
                )
                client.drop_collection(name)
            else:
                logger.info("collection already exists, skipping", collection=name)
                continue

        schema = _schema_map[name]
        schema.add_function(_bm25_funcs[name])

        client.create_collection(
            name,
            schema=schema,
            shards_num=2,
            consistency_level=_consistency[name],
        )
        logger.info("collection created", collection=name)


def create_indexes(client: MilvusClient) -> None:
    """Create all AUTOINDEX indexes on both collections, waiting for completion.

    Idempotent — skips indexes that already exist and are in ``"Finished"``
    state.
    """
    _tasks: dict[str, IndexParams] = {
        "arxiv_papers": _build_arxiv_papers_indexes(),
        "arxiv_chunks": _build_arxiv_chunks_indexes(),
    }

    for collection_name, index_params in _tasks.items():
        existing_names: set[str] = set(client.list_indexes(collection_name))
        pending = IndexParams()
        wait_list: list[str] = []

        for ip in index_params:
            if ip.index_name not in existing_names:
                pending.append(ip)
                wait_list.append(ip.index_name)
            else:
                info = client.describe_index(collection_name, ip.index_name)
                if info.get("state") != "Finished":
                    wait_list.append(ip.index_name)

        if len(pending) > 0:
            client.create_index(collection_name, index_params=pending)
            logger.info(
                "index batch submitted",
                collection=collection_name,
                count=len(pending),
            )
        else:
            logger.info("all indexes already exist", collection=collection_name)

        for idx_name in wait_list:
            _wait_for_index(client, collection_name, idx_name)
            logger.info("index ready", collection=collection_name, index=idx_name)


def ensure_collections(client: MilvusClient) -> None:
    """Create collections, build indexes, and load both into memory.

    Safe to call at startup — fully idempotent.
    """
    create_collections(client)
    create_indexes(client)
    for name in COLLECTION_NAMES:
        client.load_collection(name, timeout=_COLLECTION_LOAD_TIMEOUT)
        logger.info("collection loaded", collection=name)


def drop_collections(client: MilvusClient) -> None:
    """Drop both collections if they exist."""
    for name in reversed(COLLECTION_NAMES):
        if client.has_collection(name):
            client.drop_collection(name)
            logger.info("collection dropped", collection=name)
