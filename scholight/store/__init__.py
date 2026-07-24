"""Store: Milvus collection management + two-phase search/ingest."""

from scholight.store.client import (
    DELETE_CONSISTENCY,
    QUERY_CONSISTENCY,
    SEARCH_CONSISTENCY,
    close,
    connect,
    get_client,
    is_connected,
)
from scholight.store.concurrent import (
    insert_arxiv_chunks_concurrent,
    insert_arxiv_papers_concurrent,
)
from scholight.store.fields import (
    CHUNK_ALL_FIELDS,
    CHUNK_SEARCH_FIELDS,
    CHUNK_VECTOR_FIELDS,
    PAPER_ALL_FIELDS,
    PAPER_SEARCH_FIELDS,
    PAPER_VECTOR_FIELDS,
)
from scholight.store.ingest import (
    StoreError,
    arxiv_paper_exists,
    count_papers_without,
    delete_arxiv_paper,
    get_arxiv_chunks_by_paper,
    get_arxiv_paper,
    insert_arxiv_chunks,
    insert_arxiv_paper,
    query_papers_without,
    update_arxiv_chunk,
    update_arxiv_paper,
    upsert_arxiv_chunks,
    upsert_arxiv_papers,
)
from scholight.store.query import (
    CHUNK_OUTPUT_FIELDS,
    PAPER_OUTPUT_FIELDS,
    get_arxiv_paper_by_id,
    hybrid_search_all_chunks,
    hybrid_search_arxiv_chunks,
    hybrid_search_arxiv_papers,
    search_all_chunks,
    search_arxiv_chunks,
    search_arxiv_papers,
)
from scholight.store.schema import COLLECTION_NAMES, ensure_collections

__all__ = [
    # Client
    "DELETE_CONSISTENCY",
    "QUERY_CONSISTENCY",
    "SEARCH_CONSISTENCY",
    "close",
    "connect",
    "get_client",
    "is_connected",
    # Schema
    "COLLECTION_NAMES",
    "ensure_collections",
    # Fields — single source of truth
    "CHUNK_ALL_FIELDS",
    "CHUNK_SEARCH_FIELDS",
    "CHUNK_VECTOR_FIELDS",
    "PAPER_ALL_FIELDS",
    "PAPER_SEARCH_FIELDS",
    "PAPER_VECTOR_FIELDS",
    "PAPER_OUTPUT_FIELDS",
    "CHUNK_OUTPUT_FIELDS",
    # Ingest — errors
    "StoreError",
    # Ingest — arxiv papers
    "arxiv_paper_exists",
    "delete_arxiv_paper",
    "get_arxiv_paper",
    "insert_arxiv_paper",
    "update_arxiv_paper",
    "upsert_arxiv_papers",
    # Ingest — arxiv chunks
    "get_arxiv_chunks_by_paper",
    "insert_arxiv_chunks",
    "update_arxiv_chunk",
    "upsert_arxiv_chunks",
    # Ingest — pipeline helpers (flag-based)
    "count_papers_without",
    "query_papers_without",
    # Ingest — concurrent
    "insert_arxiv_chunks_concurrent",
    "insert_arxiv_papers_concurrent",
    # Query
    "get_arxiv_paper_by_id",
    "hybrid_search_all_chunks",
    "hybrid_search_arxiv_chunks",
    "hybrid_search_arxiv_papers",
    "search_all_chunks",
    "search_arxiv_chunks",
    "search_arxiv_papers",
]
