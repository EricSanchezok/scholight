"""Safety contracts for single-paper revision installation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pymilvus.exceptions import MilvusException

from scholight.store.ingest import StoreError
from scholight.store.ingestion import (
    IngestionSafetyError,
    get_chunk_ids,
    install_paper_chunks,
    write_metadata_papers,
)


def _chunk(index: int) -> dict[str, object]:
    return {
        "chunk_id": f"2401.00001::chunk::{index}",
        "arxiv_id": "2401.00001",
        "chunk_idx": index,
        "content_text": "content",
        "content_embedding": [0.1],
    }


def test_revision_upserts_before_exact_primary_key_delete() -> None:
    client = MagicMock()
    client.query.return_value = [
        {"chunk_id": "old", "arxiv_id": "2401.00001"},
        {"chunk_id": "2401.00001::chunk::0", "arxiv_id": "2401.00001"},
    ]
    client.get.return_value = [{"arxiv_id": "2401.00001", "version": 1}]
    calls: list[str] = []
    client.upsert.side_effect = lambda *args, **kwargs: calls.append("upsert") or {}
    client.delete.side_effect = lambda *args, **kwargs: calls.append("delete") or {}

    with patch("scholight.store.ingestion.get_client", return_value=client):
        install_paper_chunks(
            "2401.00001",
            [_chunk(0)],
            target_version=1,
            resource_flags={"has_pdf": True},
        )

    assert calls == ["upsert", "delete", "upsert"]
    assert client.delete.call_args.kwargs["ids"] == ["old"]
    assert "filter" not in client.delete.call_args.kwargs


def test_revision_never_deletes_when_new_upsert_fails() -> None:
    client = MagicMock()
    client.query.return_value = [{"chunk_id": "old", "arxiv_id": "2401.00001"}]
    client.get.return_value = [{"arxiv_id": "2401.00001", "version": 1}]
    client.upsert.side_effect = MilvusException(message="failed", code=1)

    with patch("scholight.store.ingestion.get_client", return_value=client):
        with pytest.raises(StoreError):
            install_paper_chunks(
                "2401.00001",
                [_chunk(0)],
                target_version=1,
                resource_flags={"has_pdf": True},
            )

    client.delete.assert_not_called()


def test_chunk_query_fails_closed_on_mismatched_paper() -> None:
    client = MagicMock()
    client.query.return_value = [{"chunk_id": "foreign", "arxiv_id": "2401.99999"}]

    with patch("scholight.store.ingestion.get_client", return_value=client):
        with pytest.raises(IngestionSafetyError):
            get_chunk_ids("2401.00001")


def test_metadata_never_downgrades_a_trusted_version() -> None:
    client = MagicMock()
    client.get.return_value = [
        {
            "arxiv_id": "2401.00001",
            "version": 3,
            "created": "2024-01-01",
            "updated": "2024-02-01",
            "has_latex": True,
            "has_pdf": True,
            "has_markdown": True,
            "has_chunks": True,
        }
    ]

    with patch("scholight.store.ingestion.get_client", return_value=client):
        outcomes = write_metadata_papers(
            [
                {
                    "arxiv_id": "2401.00001",
                    "version": 2,
                    "_version_available": True,
                    "title": "older",
                }
            ]
        )

    assert outcomes[0].target_version == 3
    client.upsert.assert_not_called()


def test_metadata_revision_preserves_existing_resource_flags() -> None:
    client = MagicMock()
    client.get.return_value = [
        {
            "arxiv_id": "2401.00001",
            "version": 1,
            "created": "2024-01-01",
            "updated": "2024-01-01",
            "has_latex": True,
            "has_pdf": True,
            "has_markdown": True,
            "has_chunks": True,
        }
    ]

    with patch("scholight.store.ingestion.get_client", return_value=client):
        outcomes = write_metadata_papers(
            [
                {
                    "arxiv_id": "2401.00001",
                    "version": 2,
                    "_version_available": True,
                    "title": "new",
                    "has_chunks": False,
                }
            ]
        )

    assert outcomes[0].kind == "revision"
    sent = client.upsert.call_args.kwargs["data"][0]
    assert "has_chunks" not in sent
