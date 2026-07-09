"""Unit tests for scholight.store.ingest."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pymilvus.exceptions import MilvusException

from scholight.store.ingest import (
    StoreError,
    _validate_chunk_insert,
    delete_arxiv_chunks_by_paper,
    delete_arxiv_paper,
    insert_arxiv_chunks,
    update_arxiv_chunk,
    update_arxiv_paper,
    upsert_arxiv_papers,
)

# ── update_arxiv_paper ────────────────────────────────────────────────────────


class TestUpdateArxivPaper:
    def test_update_sets_has_pdf_true(self) -> None:
        """update_arxiv_paper sends the fields in the upsert data."""
        mock_client = MagicMock()
        mock_client.upsert.return_value = {"upsert_count": 1}

        with patch("scholight.store.ingest.get_client", return_value=mock_client):
            update_arxiv_paper("2401.00001", {"has_pdf": True})

        call_kwargs = mock_client.upsert.call_args.kwargs
        sent_data = call_kwargs["data"][0]
        assert sent_data["arxiv_id"] == "2401.00001"
        assert sent_data["has_pdf"] is True

    def test_update_returns_true_on_success(self) -> None:
        """update_arxiv_paper returns True when upsert_count > 0."""
        mock_client = MagicMock()
        mock_client.upsert.return_value = {"upsert_count": 1}

        with patch("scholight.store.ingest.get_client", return_value=mock_client):
            result = update_arxiv_paper("2401.00001", {"has_pdf": True})

        assert result is True

    def test_update_returns_false_when_not_found(self) -> None:
        """update_arxiv_paper returns False when upsert_count is 0."""
        mock_client = MagicMock()
        mock_client.upsert.return_value = {"upsert_count": 0}

        with patch("scholight.store.ingest.get_client", return_value=mock_client):
            result = update_arxiv_paper("2401.00001", {"has_pdf": True})

        assert result is False

    def test_update_raises_on_milvus_error(self) -> None:
        """MilvusException is wrapped in StoreError."""
        mock_client = MagicMock()
        mock_client.upsert.side_effect = MilvusException(message="connection lost", code=1)

        with patch("scholight.store.ingest.get_client", return_value=mock_client):
            with pytest.raises(StoreError, match="Failed to update paper"):
                update_arxiv_paper("2401.00001", {"has_pdf": True})

    def test_update_no_partial_update_means_full_replace(self) -> None:
        """partial_update=True is explicitly passed (not relying on default)."""
        mock_client = MagicMock()
        mock_client.upsert.return_value = {"upsert_count": 1}

        with patch("scholight.store.ingest.get_client", return_value=mock_client):
            update_arxiv_paper("2401.00001", {"has_pdf": True})

        call_kwargs = mock_client.upsert.call_args.kwargs
        assert call_kwargs["partial_update"] is True
        assert mock_client.upsert.call_args[0][0] == "arxiv_papers"


# ── delete_arxiv_paper ────────────────────────────────────────────────────────


class TestDeleteArxivPaper:
    def test_delete_returns_true_when_paper_exists(self) -> None:
        """Returns True when query finds paper and delete succeeds."""
        mock_client = MagicMock()
        mock_client.query.return_value = [{"arxiv_id": "2401.00001"}]
        mock_client.delete.return_value = {"delete_count": 1}

        with patch("scholight.store.ingest._delete_chunks_locked", MagicMock()):
            with patch("scholight.store.ingest.get_client", return_value=mock_client):
                result = delete_arxiv_paper("2401.00001")

        assert result is True

    def test_delete_returns_false_when_paper_not_found(self) -> None:
        """Returns False when query returns empty — delete is NOT called."""
        mock_client = MagicMock()
        mock_client.query.return_value = []

        with patch("scholight.store.ingest._delete_chunks_locked", MagicMock()):
            with patch("scholight.store.ingest.get_client", return_value=mock_client):
                result = delete_arxiv_paper("2401.00001")

        assert result is False
        mock_client.delete.assert_not_called()

    def test_delete_deletes_chunks_before_paper(self) -> None:
        """_delete_chunks_locked is called BEFORE client.delete on arxiv_papers."""
        call_order: list[str] = []

        mock_client = MagicMock()
        mock_client.query.return_value = [{"arxiv_id": "2401.00001"}]

        def _record_and_return(*args: Any, **kw: Any) -> dict[str, int]:
            call_order.append("delete_paper")
            return {"delete_count": 1}

        mock_client.delete.side_effect = _record_and_return

        mock_delete_chunks = MagicMock(
            side_effect=lambda client, aid: call_order.append("delete_chunks")
        )

        with patch("scholight.store.ingest._delete_chunks_locked", mock_delete_chunks):
            with patch("scholight.store.ingest.get_client", return_value=mock_client):
                delete_arxiv_paper("2401.00001")

        assert call_order == ["delete_chunks", "delete_paper"]

    def test_delete_raises_on_milvus_error(self) -> None:
        """Any MilvusException during delete flow → StoreError."""
        mock_client = MagicMock()
        mock_client.query.side_effect = MilvusException(message="query error", code=1)

        with patch("scholight.store.ingest._delete_chunks_locked", MagicMock()):
            with patch("scholight.store.ingest.get_client", return_value=mock_client):
                with pytest.raises(StoreError, match="Failed to delete paper"):
                    delete_arxiv_paper("2401.00001")


# ── insert_arxiv_chunks ───────────────────────────────────────────────────────


class TestInsertArxivChunks:
    def test_insert_single_paper_deletes_all_chunks(self) -> None:
        """Single-paper batch calls _delete_chunks_locked (full replacement)."""
        chunks = [
            {**_make_valid_chunk(), "chunk_id": f"c{i}", "arxiv_id": "2401.00001", "chunk_idx": i}
            for i in range(3)
        ]
        mock_client = MagicMock()
        mock_client.insert.return_value = {"insert_count": 3, "ids": [1, 2, 3]}
        mock_delete_chunks = MagicMock()

        with patch("scholight.store.ingest._delete_chunks_locked", mock_delete_chunks):
            with patch("scholight.store.ingest.get_client", return_value=mock_client):
                insert_arxiv_chunks(chunks)

        mock_delete_chunks.assert_called_once_with(mock_client, "2401.00001")
        mock_client.delete.assert_not_called()

    def test_insert_multi_paper_deletes_specific_indices(self) -> None:
        """Multi-paper batch uses surgical delete with chunk_idx in filter."""
        chunks = [
            {**_make_valid_chunk(), "chunk_id": "c1", "arxiv_id": "A", "chunk_idx": 0},
            {**_make_valid_chunk(), "chunk_id": "c2", "arxiv_id": "B", "chunk_idx": 1},
            {**_make_valid_chunk(), "chunk_id": "c3", "arxiv_id": "A", "chunk_idx": 2},
        ]
        mock_client = MagicMock()
        mock_client.insert.return_value = {"insert_count": 3, "ids": [1, 2, 3]}
        mock_client.delete.return_value = {"delete_count": 0}

        with patch("scholight.store.ingest.get_client", return_value=mock_client):
            insert_arxiv_chunks(chunks)

        assert mock_client.delete.call_count == 2
        # Verify at least one delete filter uses chunk_idx surgical pattern
        filters = [str(call.kwargs["filter"]) for call in mock_client.delete.call_args_list]
        assert any("chunk_idx in" in f for f in filters)

    def test_insert_multi_paper_does_not_call_delete_chunks_locked(self) -> None:
        """Multi-paper batch does NOT call _delete_chunks_locked."""
        chunks = [
            {**_make_valid_chunk(), "chunk_id": "c1", "arxiv_id": "A", "chunk_idx": 0},
            {**_make_valid_chunk(), "chunk_id": "c2", "arxiv_id": "B", "chunk_idx": 1},
        ]
        mock_client = MagicMock()
        mock_client.insert.return_value = {"insert_count": 2, "ids": [1, 2]}
        mock_client.delete.return_value = {"delete_count": 0}
        mock_delete_chunks = MagicMock()

        with patch("scholight.store.ingest._delete_chunks_locked", mock_delete_chunks):
            with patch("scholight.store.ingest.get_client", return_value=mock_client):
                insert_arxiv_chunks(chunks)

        mock_delete_chunks.assert_not_called()

    def test_insert_empty_list_returns_zero(self) -> None:
        """Empty chunk list returns insert_count=0 without touching Milvus."""
        result = insert_arxiv_chunks([])
        assert result == {"insert_count": 0, "ids": []}


# ── upsert_arxiv_papers ───────────────────────────────────────────────────────


class TestUpsertArxivPapers:
    def test_upsert_raises_with_committed_count(self) -> None:
        """StoreError message includes how many committed before the failure."""
        total_papers = 5
        # Build full paper dicts to pass _validate_paper_full_upsert guard.
        _PAPER_TEMPLATE: dict[str, Any] = {
            "title": "test",
            "abstract": "test",
            "authors": ["T. Est"],
            "categories": ["cs.AI"],
            "created": "2024-01-01",
            "updated": "2024-01-01",
            "version": "v1",
            "updated_history": [],
            "license": "",
            "comments": "",
            "doi": "",
            "journal_ref": "",
            "acm_class": "",
            "has_latex": False,
            "has_pdf": False,
            "has_markdown": False,
            "has_chunks": False,
            "abstract_embedding": [0.0] * 1024,
            "abstract_bm25": {"0": 1.0},
        }
        papers = [{**_PAPER_TEMPLATE, "arxiv_id": f"2401.0000{i}"} for i in range(total_papers)]
        mock_client = MagicMock()

        # Side effect: succeed for first 2 batches, fail on the 3rd
        side_effects: list[dict[str, Any] | MilvusException] = [
            {"upsert_count": 1},  # batch 1
            {"upsert_count": 1},  # batch 2
            MilvusException(message="batch 3 failed", code=1),  # batch 3
            {"upsert_count": 1},  # batch 4 (unreachable)
            {"upsert_count": 1},  # batch 5 (unreachable)
        ]
        mock_client.upsert.side_effect = side_effects

        # Split into 5 batches of 1 paper each (patch batched from client.py)
        def _one_per_batch(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
            return [[item] for item in items]

        with patch("scholight.store.ingest.batched", side_effect=_one_per_batch):
            with patch("scholight.store.ingest.get_client", return_value=mock_client):
                with pytest.raises(StoreError) as exc_info:
                    upsert_arxiv_papers(papers)

        msg = str(exc_info.value)
        assert "2/5" in msg


# ── delete_arxiv_chunks_by_paper ──────────────────────────────────────────────


class TestDeleteArxivChunksByPaper:
    def test_delete_chunks_lets_storeerror_pass_through(self) -> None:
        """StoreError from _delete_chunks_locked is re-raised, not caught."""
        mock_client = MagicMock()
        mock_delete_chunks = MagicMock(side_effect=StoreError("chunk delete failure"))

        with patch("scholight.store.ingest._delete_chunks_locked", mock_delete_chunks):
            with patch("scholight.store.ingest.get_client", return_value=mock_client):
                with pytest.raises(StoreError, match="chunk delete failure"):
                    delete_arxiv_chunks_by_paper("2401.00001")

    def test_delete_chunks_returns_delete_count(self) -> None:
        """Returns the count from _delete_chunks_locked."""
        mock_client = MagicMock()
        mock_delete_chunks = MagicMock(return_value=42)

        with patch("scholight.store.ingest._delete_chunks_locked", mock_delete_chunks):
            with patch("scholight.store.ingest.get_client", return_value=mock_client):
                result = delete_arxiv_chunks_by_paper("2401.00001")

        assert result == 42
        mock_delete_chunks.assert_called_once_with(mock_client, "2401.00001")


# ── _validate_chunk_insert ─────────────────────────────────────────────────────


def _make_valid_chunk() -> dict[str, Any]:
    """Return a chunk dict that passes _validate_chunk_insert."""
    return {
        "chunk_id": "c001",
        "arxiv_id": "2401.00001",
        "chunk_idx": 0,
        "content_embedding": [0.1, 0.2, 0.3],
    }


class TestValidateChunkInsert:
    def test_valid_chunk_passes(self) -> None:
        """All required fields present → no error."""
        chunk = _make_valid_chunk()
        _validate_chunk_insert(chunk)

    def test_missing_chunk_id_raises(self) -> None:
        """chunk_id missing → StoreError."""
        chunk = _make_valid_chunk()
        del chunk["chunk_id"]
        with pytest.raises(StoreError, match="chunk_id"):
            _validate_chunk_insert(chunk)

    def test_empty_chunk_id_raises(self) -> None:
        """chunk_id='' → StoreError."""
        chunk = _make_valid_chunk()
        chunk["chunk_id"] = ""
        with pytest.raises(StoreError, match="chunk_id"):
            _validate_chunk_insert(chunk)

    def test_missing_arxiv_id_raises(self) -> None:
        """arxiv_id missing → StoreError."""
        chunk = _make_valid_chunk()
        del chunk["arxiv_id"]
        with pytest.raises(StoreError, match="arxiv_id"):
            _validate_chunk_insert(chunk)

    def test_missing_content_embedding_raises(self) -> None:
        """content_embedding=None → StoreError."""
        chunk = _make_valid_chunk()
        chunk["content_embedding"] = None
        with pytest.raises(StoreError, match="content_embedding is missing"):
            _validate_chunk_insert(chunk)

    def test_empty_content_embedding_raises(self) -> None:
        """content_embedding=[] → StoreError."""
        chunk = _make_valid_chunk()
        chunk["content_embedding"] = []
        with pytest.raises(StoreError, match="content_embedding is empty"):
            _validate_chunk_insert(chunk)

    def test_content_bm25_not_required(self) -> None:
        """content_bm25 is auto-populated by Zilliz Function — absent is fine."""
        chunk = _make_valid_chunk()
        _validate_chunk_insert(chunk)  # does not raise


# ── update_arxiv_chunk ─────────────────────────────────────────────────────────


class TestUpdateArxivChunk:
    def test_update_sets_field(self) -> None:
        """update_arxiv_chunk sends partial_update=True with chunk_id + field."""
        mock_client = MagicMock()
        mock_client.upsert.return_value = {"upsert_count": 1}

        with patch("scholight.store.ingest.get_client", return_value=mock_client):
            update_arxiv_chunk("c001", {"content_text": "hello"})

        call_kwargs = mock_client.upsert.call_args.kwargs
        sent_data = call_kwargs["data"][0]
        assert sent_data["chunk_id"] == "c001"
        assert sent_data["content_text"] == "hello"
        assert call_kwargs["partial_update"] is True
        assert mock_client.upsert.call_args[0][0] == "arxiv_chunks"

    def test_update_returns_true_on_success(self) -> None:
        """upsert_count=1 → True."""
        mock_client = MagicMock()
        mock_client.upsert.return_value = {"upsert_count": 1}

        with patch("scholight.store.ingest.get_client", return_value=mock_client):
            result = update_arxiv_chunk("c001", {"content_text": "hello"})

        assert result is True

    def test_update_returns_false_when_not_found(self) -> None:
        """upsert_count=0 → False."""
        mock_client = MagicMock()
        mock_client.upsert.return_value = {"upsert_count": 0}

        with patch("scholight.store.ingest.get_client", return_value=mock_client):
            result = update_arxiv_chunk("c001", {"content_text": "hello"})

        assert result is False

    def test_update_raises_on_milvus_error(self) -> None:
        """MilvusException → StoreError."""
        mock_client = MagicMock()
        mock_client.upsert.side_effect = MilvusException(message="connection lost", code=1)

        with patch("scholight.store.ingest.get_client", return_value=mock_client):
            with pytest.raises(StoreError, match="Failed to update chunk"):
                update_arxiv_chunk("c001", {"content_text": "hello"})


# ── insert_arxiv_chunks validation ─────────────────────────────────────────────


class TestInsertArxivChunksWithValidation:
    def test_insert_blocks_invalid_chunk(self) -> None:
        """If a chunk fails validation, insert never happens."""
        chunks = [_make_valid_chunk()]
        chunks[0]["content_embedding"] = None
        mock_client = MagicMock()

        with patch("scholight.store.ingest.get_client", return_value=mock_client):
            with pytest.raises(StoreError):
                insert_arxiv_chunks(chunks)

        mock_client.insert.assert_not_called()
