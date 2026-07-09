"""Single source of truth for arxiv_papers and arxiv_chunks field lists.

All other modules should import their field subsets from here rather than
maintaining independent copies.  When the schema changes, update only this file.
"""

from __future__ import annotations

from typing import Final

# ── Papers: complete field list (order matches ARXIV_PAPERS_SCHEMA) ───────────

PAPER_ALL_FIELDS: Final[list[str]] = [
    "arxiv_id",
    "title",
    "authors",
    "abstract",
    "categories",
    "created",
    "updated",
    "version",
    "updated_history",
    "license",
    "comments",
    "doi",
    "journal_ref",
    "acm_class",
    "has_latex",
    "has_pdf",
    "has_markdown",
    "has_chunks",
    "abstract_embedding",
    "abstract_bm25",
]

# ── Papers: vector field names ────────────────────────────────────────────────

PAPER_VECTOR_FIELDS: Final[frozenset[str]] = frozenset({"abstract_embedding", "abstract_bm25"})

# ── Papers: search output fields — minimal set to reduce Zilliz Read vCU ──────
#
# ``abstract`` is EXCLUDED because it is a 16 KB payload field.  Returning it
# in every search result would balloon data_returned_size and inflate vCU.
# Callers that need the full abstract should fetch it with a secondary O(1)
# ``client.query()`` by arxiv_id.

PAPER_SEARCH_FIELDS: Final[list[str]] = [
    "arxiv_id",
    "title",
    "authors",
    "categories",
    "created",
    "updated",
    "version",
    "updated_history",
    "license",
    "comments",
    "doi",
    "journal_ref",
    "acm_class",
    "has_latex",
    "has_pdf",
    "has_markdown",
    "has_chunks",
]

# ── Papers: search fields with dense embedding (for L1 re-ranking) ────────────

PAPER_SEARCH_WITH_EMBEDDING: Final[list[str]] = [
    *PAPER_SEARCH_FIELDS,
    "abstract_embedding",
]

# ── Chunks: complete field list (order matches ARXIV_CHUNKS_SCHEMA) ───────────

CHUNK_ALL_FIELDS: Final[list[str]] = [
    "chunk_id",
    "arxiv_id",
    "chunk_idx",
    "content_text",
    "content_embedding",
    "content_bm25",
]

# ── Chunks: vector field names ────────────────────────────────────────────────

CHUNK_VECTOR_FIELDS: Final[frozenset[str]] = frozenset({"content_embedding", "content_bm25"})

# ── Chunks: search output fields — minimal set to reduce Zilliz Read vCU ──────
#
# ``content_text`` (16 KB) and ``paper_title`` are EXCLUDED.
# Callers that need full text should fetch via ``client.query()`` by chunk_id.

CHUNK_SEARCH_FIELDS: Final[list[str]] = [
    "chunk_id",
    "arxiv_id",
    "chunk_idx",
]

# ── Convenience aliases (backward-compatible) ─────────────────────────────────

PAPER_OUTPUT_FIELDS: Final[list[str]] = PAPER_SEARCH_FIELDS
CHUNK_OUTPUT_FIELDS: Final[list[str]] = CHUNK_SEARCH_FIELDS
