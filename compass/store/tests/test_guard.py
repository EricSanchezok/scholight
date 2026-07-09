"""First-principles tests for _validate_paper_full_upsert.

No mocks — the guard is a pure function that returns or raises.
Tests what the guard MUST enforce, not what it happens to enforce today.
"""

from __future__ import annotations

from typing import Any

import pytest

from compass.store.ingest import StoreError, _validate_paper_full_upsert

# ── Helpers ───────────────────────────────────────────────────────────────────

_TEMPLATE: dict[str, Any] = {
    "arxiv_id": "2401.00001",
    "title": "A Test Paper",
    "abstract": "Some abstract.",
    "authors": ["Alice", "Bob"],
    "categories": ["cs.AI"],
    "created": "2024-01-15",
    "updated": "2024-06-01",
    "version": "v2",
    "updated_history": ["2024-03-01"],
    "license": "CC BY 4.0",
    "comments": "10 pages",
    "doi": "10.1234/foo",
    "journal_ref": "JMLR 2024",
    "acm_class": "I.2.6",
    "has_latex": True,
    "has_pdf": True,
    "has_markdown": True,
    "has_chunks": False,
    "abstract_embedding": [0.0] * 1024,
    "abstract_bm25": {"0": 0.0},
}


# ── Core contract ─────────────────────────────────────────────────────────────


class TestGuardRejects:
    """Contract: the guard MUST raise StoreError when data is incomplete."""

    def test_missing_single_scalar(self) -> None:
        """Any missing scalar field → StoreError immediately."""
        paper = {**_TEMPLATE}
        del paper["title"]
        with pytest.raises(StoreError, match="title"):
            _validate_paper_full_upsert(paper)

    def test_missing_multiple_scalars(self) -> None:
        """Minimal dict (only arxiv_id) → StoreError naming ALL missing fields."""
        paper = {"arxiv_id": "2401.00001"}
        with pytest.raises(StoreError, match="missing"):
            _validate_paper_full_upsert(paper)

    def test_empty_embedding_list(self) -> None:
        """abstract_embedding=[] → StoreError (present but empty)."""
        paper = {**_TEMPLATE, "abstract_embedding": []}
        with pytest.raises(StoreError, match="abstract_embedding"):
            _validate_paper_full_upsert(paper)

    def test_empty_sparse_dict(self) -> None:
        """abstract_bm25={} → StoreError (present but empty)."""
        paper = {**_TEMPLATE, "abstract_bm25": {}}
        with pytest.raises(StoreError, match="abstract_bm25"):
            _validate_paper_full_upsert(paper)

    def test_vector_key_entirely_absent(self) -> None:
        """abstract_embedding key not in dict at all → StoreError.

        Not the same as abstract_embedding=[] — the key simply does not exist.
        Without this check, paper.get('abstract_embedding') returns None and
        isinstance(None, list) is False, silently passing the guard.  This test
        exists because we discovered this gap during audit.
        """
        paper = {**_TEMPLATE}
        del paper["abstract_embedding"]
        with pytest.raises(StoreError, match="abstract_embedding"):
            _validate_paper_full_upsert(paper)

    def test_all_vectors_empty(self) -> None:
        """All vector fields empty → StoreError."""
        paper = {**_TEMPLATE, "abstract_embedding": [], "abstract_bm25": {}}
        with pytest.raises(StoreError):
            _validate_paper_full_upsert(paper)


class TestGuardErrorMessages:
    """Contract: error messages must be actionable for debugging."""

    def test_arxiv_id_in_error(self) -> None:
        """Error message includes the specific arxiv_id."""
        full = {**_TEMPLATE, "arxiv_id": "2401.00042"}
        del full["title"]  # trigger scalar missing
        with pytest.raises(StoreError, match=r"2401\.00042"):
            _validate_paper_full_upsert(full)

    def test_hints_partial_update_path(self) -> None:
        """Error mentions update_arxiv_paper as the safe alternative."""
        paper = {"arxiv_id": "2401.00001"}
        with pytest.raises(StoreError, match="update_arxiv_paper"):
            _validate_paper_full_upsert(paper)

    def test_names_missing_vector_field(self) -> None:
        """Empty vector error names which field is empty."""
        paper = {**_TEMPLATE, "abstract_embedding": []}
        with pytest.raises(StoreError, match="abstract_embedding"):
            _validate_paper_full_upsert(paper)


class TestGuardPasses:
    """Contract: a complete valid dict must pass silently."""

    def test_complete_paper_passes(self) -> None:
        """All fields present + vectors non-empty → no error."""
        _validate_paper_full_upsert(_TEMPLATE)  # does not raise

    def test_zero_vector_passes(self) -> None:
        """A zero-vector of correct dimension is valid (not empty)."""
        paper = {**_TEMPLATE, "abstract_embedding": [0.0] * 1024}
        _validate_paper_full_upsert(paper)  # does not raise
