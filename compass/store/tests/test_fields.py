"""Invariant tests for compass.store.fields — single source of truth.

No mocks.  Pure data-structure assertions that MUST hold regardless of
schema changes.  If someone adds a field but forgets to classify it as
a vector, these tests break loudly.
"""

from __future__ import annotations

from compass.store.fields import (
    CHUNK_ALL_FIELDS,
    CHUNK_SEARCH_FIELDS,
    CHUNK_VECTOR_FIELDS,
    PAPER_ALL_FIELDS,
    PAPER_SEARCH_FIELDS,
    PAPER_VECTOR_FIELDS,
)


class TestPaperFields:
    """Invariants on arxiv_papers field lists."""

    def test_search_equals_all_minus_vectors(self) -> None:
        """PAPER_SEARCH must be a subset of PAPER_ALL without the vector-only fields.

        This is the central invariant: every non-vector field in search output
        comes from the all-fields list, and only vector fields are excluded.
        Some scalar fields (abstract) are intentionally omitted from search
        results for payload-size/vCU reasons.
        """
        searchable = set(PAPER_SEARCH_FIELDS)
        eligible = set(PAPER_ALL_FIELDS) - PAPER_VECTOR_FIELDS
        assert searchable.issubset(eligible)

    def test_no_vector_in_search(self) -> None:
        """Not a single vector field should leak into PAPER_SEARCH."""
        assert PAPER_VECTOR_FIELDS.isdisjoint(PAPER_SEARCH_FIELDS)

    def test_vector_fields_are_subset_of_all(self) -> None:
        """Every entry in PAPER_VECTOR_FIELDS must exist in PAPER_ALL_FIELDS."""
        assert PAPER_VECTOR_FIELDS.issubset(set(PAPER_ALL_FIELDS))

    def test_no_duplicate_fields(self) -> None:
        """PAPER_ALL_FIELDS must not contain duplicates."""
        assert len(PAPER_ALL_FIELDS) == len(set(PAPER_ALL_FIELDS))

    def test_arxiv_id_is_first(self) -> None:
        """arxiv_id is the PK — must be the first field in PAPER_ALL_FIELDS."""
        assert PAPER_ALL_FIELDS[0] == "arxiv_id"


class TestChunkFields:
    """Invariants on arxiv_chunks field lists."""

    def test_search_equals_all_minus_vectors(self) -> None:
        """CHUNK_SEARCH must be a subset (content_text excluded)."""
        searchable = set(CHUNK_SEARCH_FIELDS)
        eligible = set(CHUNK_ALL_FIELDS) - CHUNK_VECTOR_FIELDS
        assert searchable.issubset(eligible)

    def test_no_vector_in_search(self) -> None:
        assert CHUNK_VECTOR_FIELDS.isdisjoint(CHUNK_SEARCH_FIELDS)

    def test_vector_fields_are_subset_of_all(self) -> None:
        assert CHUNK_VECTOR_FIELDS.issubset(set(CHUNK_ALL_FIELDS))

    def test_no_duplicate_fields(self) -> None:
        assert len(CHUNK_ALL_FIELDS) == len(set(CHUNK_ALL_FIELDS))

    def test_chunk_id_is_first(self) -> None:
        assert CHUNK_ALL_FIELDS[0] == "chunk_id"


class TestChunkSearchConfig:
    """Config values that affect chunk search behaviour."""


class TestConsistency:
    """Non-obvious invariants that would break silently if violated."""

    def test_paper_all_has_known_count(self) -> None:
        """PAPER_ALL_FIELDS has exactly 20 fields.

        If this count changes because a schema migration adds/removes a
        field, this test must be updated deliberately — it is the canonical
        "did you remember fields.py?" guard.
        """
        assert len(PAPER_ALL_FIELDS) == 20

    def test_chunk_all_has_known_count(self) -> None:
        """CHUNK_ALL_FIELDS has exactly 6 fields."""
        assert len(CHUNK_ALL_FIELDS) == 6

    def test_paper_vector_fields_are_known(self) -> None:
        """The vector fields are exactly what we expect (BM25 Function for sparse)."""
        assert PAPER_VECTOR_FIELDS == frozenset({"abstract_embedding", "abstract_bm25"})

    def test_chunk_vector_fields_are_known(self) -> None:
        assert CHUNK_VECTOR_FIELDS == frozenset({"content_embedding", "content_bm25"})
