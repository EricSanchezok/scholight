from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scholight.scheduler.arxiv_paper_sync import (
    _embed_and_ingest,
    _parse_api_entry,
    _write_synced_papers,
)
from scholight.scheduler.base import BaseDaemon, BatchResult
from scholight.scheduler.chunk_ingest import ChunkIngestDaemon
from scholight.scheduler.md_parse import MdParseDaemon
from scholight.scheduler.pdf_download import PdfDownloadDaemon
from scholight.storage import storage


class CheckpointDaemon(BaseDaemon):
    name = "test_checkpoint"
    sleep_interval = 1
    batch_size = 1

    def process_batch(self) -> BatchResult:
        return BatchResult()


class TestMetadataSync:
    def test_unchanged_paper_preserves_resource_flags(self) -> None:
        paper = _paper("2401.00001", version=2)
        client = MagicMock()
        client.get.return_value = [
            {
                "arxiv_id": paper["arxiv_id"],
                "created": paper["created"],
                "updated": paper["updated"],
                "version": paper["version"],
                **dict.fromkeys(_RESOURCE_FLAGS, True),
            }
        ]

        with (
            patch("scholight.scheduler.arxiv_paper_sync.get_client", return_value=client),
            patch(
                "scholight.scheduler.arxiv_paper_sync.delete_arxiv_chunks_by_paper"
            ) as delete_chunks,
            patch.object(storage, "remove_paper_dir") as remove_paper_dir,
        ):
            _write_synced_papers([paper])

        update = client.upsert.call_args.kwargs["data"][0]
        output_fields = set(client.get.call_args.kwargs["output_fields"])
        assert (
            client.upsert.call_args.kwargs["partial_update"] is True
            and not any(key.startswith("has_") for key in update)
            and output_fields == {"arxiv_id", "created", "updated", "version", *_RESOURCE_FLAGS}
            and delete_chunks.call_count == 0
            and remove_paper_dir.call_count == 0
        )

    def test_changed_version_resets_resources_after_update(self) -> None:
        paper = _paper("2401.00001", version=2)
        call_order: list[str] = []
        client = MagicMock()
        client.get.return_value = [
            {
                "arxiv_id": paper["arxiv_id"],
                "created": "2023-12-31",
                "updated": "2024-01-01",
                "version": 1,
                **dict.fromkeys(_RESOURCE_FLAGS, True),
            }
        ]
        client.upsert.side_effect = lambda *args, **kwargs: call_order.append("update")

        with (
            patch("scholight.scheduler.arxiv_paper_sync.get_client", return_value=client),
            patch(
                "scholight.scheduler.arxiv_paper_sync.delete_arxiv_chunks_by_paper",
                side_effect=lambda arxiv_id: call_order.append("delete_chunks"),
            ) as delete_chunks,
            patch.object(
                storage,
                "remove_paper_dir",
                side_effect=lambda arxiv_id, created: call_order.append("remove_local"),
            ) as remove_paper_dir,
        ):
            _write_synced_papers([paper])

        first_update = client.upsert.call_args_list[0].kwargs["data"][0]
        final_update = client.upsert.call_args_list[1].kwargs["data"][0]
        assert (
            set(first_update) == {"arxiv_id", *_RESOURCE_FLAGS}
            and [first_update[key] for key in _RESOURCE_FLAGS] == [False, False, False, False]
            and final_update["version"] == 2
            and [final_update[key] for key in _RESOURCE_FLAGS] == [False, False, False, False]
            and call_order == ["update", "delete_chunks", "remove_local", "update"]
            and delete_chunks.call_args.args == (paper["arxiv_id"],)
            and remove_paper_dir.call_args.args == (paper["arxiv_id"], "2023-12-31")
        )

    def test_updated_change_is_used_when_version_is_unavailable(self) -> None:
        paper = _paper("2401.00001", version=1)
        paper["_version_available"] = False
        client = MagicMock()
        client.get.return_value = [
            {
                "arxiv_id": paper["arxiv_id"],
                "created": paper["created"],
                "updated": "2024-01-01",
                "version": 3,
                **dict.fromkeys(_RESOURCE_FLAGS, True),
            }
        ]

        with (
            patch("scholight.scheduler.arxiv_paper_sync.get_client", return_value=client),
            patch("scholight.scheduler.arxiv_paper_sync.delete_arxiv_chunks_by_paper"),
            patch.object(storage, "remove_paper_dir"),
        ):
            _write_synced_papers([paper])

        update = client.upsert.call_args.kwargs["data"][0]
        assert [update[key] for key in _RESOURCE_FLAGS] == [False, False, False, False]

    def test_update_failure_preserves_chunks_and_local_resources(self) -> None:
        paper = _paper("2401.00001", version=2)
        client = MagicMock()
        client.get.return_value = [
            {
                "arxiv_id": paper["arxiv_id"],
                "created": paper["created"],
                "updated": "2024-01-01",
                "version": 1,
                **dict.fromkeys(_RESOURCE_FLAGS, True),
            }
        ]
        client.upsert.side_effect = RuntimeError("transient update failure")

        with (
            patch("scholight.scheduler.arxiv_paper_sync.get_client", return_value=client),
            patch(
                "scholight.scheduler.arxiv_paper_sync.delete_arxiv_chunks_by_paper"
            ) as delete_chunks,
            patch.object(storage, "remove_paper_dir") as remove_paper_dir,
            pytest.raises(RuntimeError, match="transient update failure"),
        ):
            _write_synced_papers([paper])

        assert delete_chunks.call_count == 0 and remove_paper_dir.call_count == 0

    def test_local_cleanup_failure_is_logged_after_invalidation(self) -> None:
        paper = _paper("2401.00001", version=2)
        client = MagicMock()
        client.get.return_value = [
            {
                "arxiv_id": paper["arxiv_id"],
                "created": paper["created"],
                "updated": "2024-01-01",
                "version": 1,
                **dict.fromkeys(_RESOURCE_FLAGS, True),
            }
        ]

        with (
            patch("scholight.scheduler.arxiv_paper_sync.get_client", return_value=client),
            patch("scholight.scheduler.arxiv_paper_sync.delete_arxiv_chunks_by_paper"),
            patch.object(storage, "remove_paper_dir", side_effect=OSError("permission denied")),
            patch("scholight.scheduler.arxiv_paper_sync.logger.warning") as warning,
        ):
            _write_synced_papers([paper])

        assert client.upsert.call_count == 1 and warning.call_count == 1

    @pytest.mark.asyncio
    async def test_new_paper_uses_full_insert_with_defaults(self) -> None:
        paper = _paper("2401.00002", version=1)
        client = MagicMock()
        client.get.return_value = []

        with (
            patch("scholight.scheduler.arxiv_paper_sync.get_client", return_value=client),
            patch("scholight.scheduler.arxiv_paper_sync.Embedder") as embedder_cls,
            patch(
                "scholight.scheduler.arxiv_paper_sync.insert_arxiv_papers_concurrent"
            ) as insert_papers,
        ):
            embedder_cls.return_value.__aenter__ = AsyncMock()
            embedder_cls.return_value.__aexit__ = AsyncMock()
            embedder_cls.return_value.embed_many = AsyncMock(return_value=[[0.1, 0.2]])
            await _embed_and_ingest([paper])

        inserted = insert_papers.call_args.args[0][0]
        assert [inserted[key] for key in _RESOURCE_FLAGS] == [False, False, False, False]
        client.upsert.assert_not_called()

    def test_api_fallback_preserves_authoritative_version_and_history(self) -> None:
        paper = _parse_api_entry(
            """
            <id>http://arxiv.org/abs/2401.00001</id>
            <updated>2024-02-03T00:00:00Z</updated>
            <published>2024-01-01T00:00:00Z</published>
            <title>Fallback title</title>
            <summary>Fallback abstract</summary>
            <author><name>Author</name></author>
            <category term="cs.AI"/>
            """
        )
        assert paper is not None
        client = MagicMock()
        client.get.return_value = [
            {
                "arxiv_id": "2401.00001",
                "created": "2024-01-01",
                "updated": "2024-02-03",
                "version": 3,
                **dict.fromkeys(_RESOURCE_FLAGS, True),
            }
        ]

        with patch("scholight.scheduler.arxiv_paper_sync.get_client", return_value=client):
            _write_synced_papers([paper])

        update = client.upsert.call_args.kwargs["data"][0]
        assert "version" not in update
        assert "updated_history" not in update
        assert "license" not in update
        assert "acm_class" not in update

    def test_api_version_suffix_updates_authoritative_version(self) -> None:
        paper = _parse_api_entry(
            """
            <id>http://arxiv.org/abs/2401.00001v4</id>
            <updated>2024-03-01T00:00:00Z</updated>
            <published>2024-01-01T00:00:00Z</published>
            <title>Version four</title>
            <summary>Abstract</summary>
            """
        )
        assert paper is not None
        assert paper["arxiv_id"] == "2401.00001"
        assert paper["version"] == 4
        assert paper["_version_available"] is True
        assert "version" in paper["_metadata_fields"]


class TestVersionedCheckpoints:
    def test_checkpoint_key_uses_version_and_updated(self) -> None:
        assert CheckpointDaemon._checkpoint_key("2401.00001", 2, "2024-02-03") == (
            "2401.00001\tversion=2\tupdated=2024-02-03"
        )
        assert CheckpointDaemon._checkpoint_key("2401.00001", None, "2024-02-03") == (
            "2401.00001\tupdated=2024-02-03"
        )

    def test_legacy_id_matches_only_without_version_metadata(self) -> None:
        entries = {"2401.00001"}
        assert CheckpointDaemon._is_checkpointed(entries, "2401.00001", None, "") is True
        assert CheckpointDaemon._is_checkpointed(entries, "2401.00001", 2, "2024-02-03") is False

    def test_legacy_checkpoint_file_remains_readable(self, tmp_path: Path) -> None:
        checkpoint = tmp_path / "done.txt"
        checkpoint.write_text("2401.00001\n2401.00002\tversion=2\n", encoding="utf-8")

        assert CheckpointDaemon._read_ids(checkpoint) == {
            "2401.00001",
            "2401.00002\tversion=2",
        }


@pytest.mark.parametrize(
    ("daemon", "process_path", "process_result", "update_path"),
    [
        (PdfDownloadDaemon(), "_download_one", "pdf", "scholight.scheduler.pdf_download"),
        (MdParseDaemon(), "_parse_one", True, "scholight.scheduler.md_parse"),
        (ChunkIngestDaemon(), "_process_one", True, "scholight.scheduler.chunk_ingest"),
    ],
)
def test_daemons_reprocess_new_version(
    daemon: BaseDaemon,
    process_path: str,
    process_result: str | bool,
    update_path: str,
) -> None:
    paper = {
        "arxiv_id": "2401.00001",
        "created": "2024-01-01",
        "updated": "2024-02-03",
        "version": 2,
        "has_pdf": True,
    }
    daemon._log = MagicMock()

    with (
        patch.object(daemon, "_fetch_work", return_value=[paper]),
        patch.object(daemon, "_load_checkpoint", return_value={"2401.00001"}),
        patch.object(daemon, "_load_failed_checkpoint", return_value=set()),
        patch.object(daemon, "_generation_is_current", return_value=True),
        patch.object(storage, "generation_lock", return_value=nullcontext()),
        patch.object(daemon, process_path, return_value=process_result) as process_one,
        patch.object(daemon, "_save_checkpoint") as save_checkpoint,
        patch(f"{update_path}.update_arxiv_paper"),
    ):
        result = daemon.process_batch()

    assert result.processed == 1
    process_one.assert_called_once()
    save_checkpoint.assert_called_once_with("2401.00001", 2, "2024-02-03")


@pytest.mark.parametrize(
    ("daemon", "process_path", "update_path"),
    [
        (PdfDownloadDaemon(), "_download_one", "scholight.scheduler.pdf_download"),
        (MdParseDaemon(), "_parse_one", "scholight.scheduler.md_parse"),
        (ChunkIngestDaemon(), "_process_one", "scholight.scheduler.chunk_ingest"),
    ],
)
def test_daemons_discard_stale_generation_before_processing(
    daemon: BaseDaemon, process_path: str, update_path: str
) -> None:
    paper = {
        "arxiv_id": "2401.00001",
        "created": "2024-01-01",
        "updated": "2024-02-03",
        "version": 2,
        "has_pdf": True,
    }
    daemon._log = MagicMock()

    with (
        patch.object(daemon, "_fetch_work", return_value=[paper]),
        patch.object(daemon, "_load_checkpoint", return_value=set()),
        patch.object(daemon, "_load_failed_checkpoint", return_value=set()),
        patch.object(daemon, "_generation_is_current", return_value=False),
        patch.object(storage, "generation_lock", return_value=nullcontext()),
        patch.object(daemon, process_path) as process_one,
        patch.object(daemon, "_save_checkpoint") as save_checkpoint,
        patch(f"{update_path}.update_arxiv_paper") as update_paper,
    ):
        result = daemon.process_batch()

    assert result.skipped == 1
    process_one.assert_not_called()
    update_paper.assert_not_called()
    save_checkpoint.assert_not_called()


def _paper(arxiv_id: str, version: int) -> dict[str, object]:
    return {
        "arxiv_id": arxiv_id,
        "title": "Title",
        "abstract": "Abstract",
        "authors": ["Author"],
        "categories": ["cs.AI"],
        "created": "2024-01-01",
        "updated": "2024-02-03",
        "version": version,
        "updated_history": ["2024-01-01", "2024-02-03"],
        "license": "",
        "comments": "",
        "doi": "",
        "journal_ref": "",
        "acm_class": "",
        "abstract_embedding": [],
        "abstract_bm25": {},
        **dict.fromkeys(_RESOURCE_FLAGS, False),
    }


_RESOURCE_FLAGS = ("has_latex", "has_pdf", "has_markdown", "has_chunks")
