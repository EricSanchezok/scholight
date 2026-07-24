"""Safety contracts for single-paper revision installation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pymilvus.exceptions import MilvusException

from scholight.store.ingest import StoreError
from scholight.store.ingestion import (
    MAX_PAPER_CHUNKS,
    IngestionSafetyError,
    get_chunk_ids,
    install_paper_chunks,
    write_metadata_papers,
)
from scholight.store.tests.fake_ingestion_client import FakeIngestionClient


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


def test_metadata_updates_are_batched_by_identical_field_set() -> None:
    client = MagicMock()
    client.get.return_value = [
        {
            "arxiv_id": "2401.00001",
            "version": 1,
            "created": "2024-01-01",
            "updated": "2024-01-01",
            "has_latex": False,
            "has_pdf": False,
            "has_markdown": False,
            "has_chunks": False,
        },
        {
            "arxiv_id": "2401.00002",
            "version": 1,
            "created": "2024-01-01",
            "updated": "2024-01-01",
            "has_latex": False,
            "has_pdf": False,
            "has_markdown": False,
            "has_chunks": False,
        },
    ]

    with patch("scholight.store.ingestion.get_client", return_value=client):
        write_metadata_papers(
            [
                {
                    "arxiv_id": "2401.00001",
                    "version": 1,
                    "_version_available": True,
                    "_metadata_fields": {"title"},
                    "title": "title only",
                },
                {
                    "arxiv_id": "2401.00002",
                    "version": 1,
                    "_version_available": True,
                    "_metadata_fields": {"title", "abstract"},
                    "title": "title and abstract",
                    "abstract": "abstract",
                },
            ]
        )

    batches = [call.kwargs["data"] for call in client.upsert.call_args_list]
    assert len(batches) == 2
    assert all(len({frozenset(row) for row in batch}) == 1 for batch in batches)


def test_multi_batch_revision_finishes_all_upserts_before_exact_delete() -> None:
    client = FakeIngestionClient()
    client.papers["2401.00001"] = {"arxiv_id": "2401.00001", "version": 1}
    client.chunks["old"] = {"chunk_id": "old", "arxiv_id": "2401.00001"}

    with patch("scholight.store.ingestion.get_client", return_value=client):
        install_paper_chunks(
            "2401.00001",
            [_chunk(index) for index in range(1001)],
            target_version=1,
            resource_flags={"has_pdf": True},
        )

    writes = [
        (operation, collection)
        for operation, collection, _details in client.operations
        if operation in {"upsert", "delete"}
    ]
    assert writes == [
        ("upsert", "arxiv_chunks"),
        ("upsert", "arxiv_chunks"),
        ("delete", "arxiv_chunks"),
        ("upsert", "arxiv_papers"),
    ]


def test_second_chunk_batch_failure_performs_zero_deletes() -> None:
    client = FakeIngestionClient()
    client.papers["2401.00001"] = {"arxiv_id": "2401.00001", "version": 1}
    client.chunks["old"] = {"chunk_id": "old", "arxiv_id": "2401.00001"}
    client.fail_chunk_upsert_call = 2

    with (
        patch("scholight.store.ingestion.get_client", return_value=client),
        pytest.raises(StoreError),
    ):
        install_paper_chunks(
            "2401.00001",
            [_chunk(index) for index in range(1001)],
            target_version=1,
            resource_flags={"has_pdf": True},
        )

    assert all(operation != "delete" for operation, _collection, _details in client.operations)


def test_too_many_existing_chunks_fails_before_any_write() -> None:
    client = FakeIngestionClient()
    client.chunks = {
        f"chunk-{index}": {
            "chunk_id": f"chunk-{index}",
            "arxiv_id": "2401.00001",
        }
        for index in range(MAX_PAPER_CHUNKS + 1)
    }

    with (
        patch("scholight.store.ingestion.get_client", return_value=client),
        pytest.raises(IngestionSafetyError, match="safe chunk replacement limit"),
    ):
        get_chunk_ids("2401.00001")

    assert all(operation != "delete" for operation, _collection, _details in client.operations)


def test_too_many_new_chunks_fails_before_store_access() -> None:
    with (
        patch("scholight.store.ingestion.get_client") as get_client,
        pytest.raises(IngestionSafetyError, match="between 1 and 10000"),
    ):
        install_paper_chunks(
            "2401.00001",
            [_chunk(index) for index in range(MAX_PAPER_CHUNKS + 1)],
            target_version=1,
            resource_flags={"has_pdf": True},
        )

    get_client.assert_not_called()


def test_version_change_before_install_performs_zero_writes() -> None:
    client = FakeIngestionClient()
    client.papers["2401.00001"] = {"arxiv_id": "2401.00001", "version": 2}

    with (
        patch("scholight.store.ingestion.get_client", return_value=client),
        pytest.raises(IngestionSafetyError, match="version changed"),
    ):
        install_paper_chunks(
            "2401.00001",
            [_chunk(0)],
            target_version=1,
            resource_flags={"has_pdf": True},
        )

    assert all(
        operation not in {"upsert", "delete"}
        for operation, _collection, _details in client.operations
    )


def test_delete_failure_does_not_clear_has_chunks() -> None:
    client = FakeIngestionClient()
    client.papers["2401.00001"] = {
        "arxiv_id": "2401.00001",
        "version": 1,
        "has_chunks": True,
    }
    client.chunks["old"] = {"chunk_id": "old", "arxiv_id": "2401.00001"}
    client.fail_next_chunk_delete = True

    with (
        patch("scholight.store.ingestion.get_client", return_value=client),
        pytest.raises(StoreError),
    ):
        install_paper_chunks(
            "2401.00001",
            [_chunk(0)],
            target_version=1,
            resource_flags={"has_pdf": True},
        )

    assert client.papers["2401.00001"]["has_chunks"] is True
