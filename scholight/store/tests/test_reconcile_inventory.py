"""Paper inventory reconciliation does not move whole-corpus vectors or infer from counts."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from scholight.models.ingestion_target import digest_json
from scholight.store.archive_io import ArchiveLocation
from scholight.store.reconcile_counts import checked_proofs
from scholight.store.reconcile_inventory import build_delta, scan_inventory
from scholight.store.tests.test_archive import IteratorStub


class InventoryClient:
    def __init__(self, rows: list[dict[str, Any]], identity: int) -> None:
        self.rows = rows
        self.identity = identity

    def describe_collection(self, name: str) -> dict[str, Any]:
        assert name == "arxiv_papers"
        return {"collection_id": self.identity}

    def query(self, name: str, **kwargs: Any) -> list[dict[str, int]]:
        assert name == "arxiv_papers" and kwargs["output_fields"] == ["count(*)"]
        return [{"count(*)": len(self.rows)}]

    def query_iterator(self, name: str, **kwargs: Any) -> IteratorStub:
        assert name == "arxiv_papers"
        assert kwargs["output_fields"] == ["arxiv_id", "version", "updated", "created"]
        return IteratorStub(self.rows, kwargs["iterator_cp_file"])


def row(pk: str, version: int, date: str = "2026-09-01") -> dict[str, Any]:
    return {"arxiv_id": pk, "version": version, "updated": date, "created": "2026-08-01"}


def inventory(
    client: InventoryClient, path: Path, *, count_duplicate_ids: tuple[str, ...] = ()
) -> dict[str, Any]:
    return scan_inventory(
        client,
        str(path),
        expected_uri=f"https://{client.identity}.invalid",
        expected_id=str(client.identity),
        frozen=True,
        workspace=path.parent / "scratch",
        count_duplicate_ids=count_duplicate_ids,
    )


def test_delta_finds_real_ids_revisions_and_preserves_newer_destination(tmp_path: Path) -> None:
    source = InventoryClient([row("c", 1), row("a", 2), row("b", 1)], 1)
    target = InventoryClient([row("a", 1), row("b", 3)], 2)
    inventory(source, tmp_path / "source")
    inventory(target, tmp_path / "target")
    result = build_delta(
        str(tmp_path / "source"),
        str(tmp_path / "target"),
        str(tmp_path / "plan"),
        workspace=tmp_path / "scratch",
    )
    assert result["counts"] == {"insert": 1, "update": 1, "target_newer": 1, "unchanged": 0}
    assert result["candidates"] == 2
    assert source.rows[0] == row("c", 1)
    assert (
        build_delta(
            str(tmp_path / "source"),
            str(tmp_path / "target"),
            str(tmp_path / "plan"),
            workspace=tmp_path / "scratch",
        )
        == result
    )


def test_scan_upload_failure_resumes_only_committed_iterator_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scholight.store.reconcile_inventory._SHARD_ROWS", 4, raising=False)
    source = InventoryClient([row(str(i), 1) for i in range(5)], 1)
    upload = ArchiveLocation.upload
    calls = 0

    def interrupted(location: ArchiveLocation, path: Path, name: str) -> None:
        nonlocal calls
        if name.endswith(".parquet"):
            calls += 1
            if calls == 2:
                raise OSError("upload failed")
        upload(location, path, name)

    with patch.object(ArchiveLocation, "upload", interrupted), pytest.raises(OSError):
        inventory(source, tmp_path / "source")
    result = inventory(source, tmp_path / "source")
    assert result["rows"] == 5
    assert result["complete"]


def test_scan_coalesces_small_reads_and_commits_partial_final_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scholight.store.reconcile_inventory._SHARD_ROWS", 4, raising=False)
    source = InventoryClient([row(str(i), 1) for i in range(9)], 1)
    result = inventory(source, tmp_path / "source")
    assert [part["rows"] for part in result["shards"]] == [4, 4, 1]


def test_uncommitted_buffer_is_reread_after_iterator_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scholight.store.reconcile_inventory._SHARD_ROWS", 4, raising=False)
    source = InventoryClient([row(str(i), 1) for i in range(9)], 1)
    original = IteratorStub.next

    def interrupted(iterator: IteratorStub) -> list[dict[str, Any]]:
        if iterator.position == 6:
            raise OSError("iterator disconnected after an uncommitted read")
        return original(iterator)

    with patch.object(IteratorStub, "next", interrupted), pytest.raises(OSError):
        inventory(source, tmp_path / "source")
    saved = ArchiveLocation(str(tmp_path / "source")).read_json("manifest.json")
    assert saved["rows"] == 4
    resumed = inventory(source, tmp_path / "source")
    assert resumed["complete"] and resumed["rows"] == 9


def test_corrupt_or_duplicate_inventory_is_not_a_plan(tmp_path: Path) -> None:
    source = InventoryClient([row("a", 1), row("a", 1)], 1)
    with pytest.raises(ValueError, match="Duplicate"):
        inventory(source, tmp_path / "source")
    target = InventoryClient([row("a", 1)], 2)
    inventory(target, tmp_path / "target")
    next((tmp_path / "target").glob("*.parquet")).write_bytes(b"broken")
    with pytest.raises(ValueError):
        build_delta(
            str(tmp_path / "target"),
            str(tmp_path / "target"),
            str(tmp_path / "plan"),
            workspace=tmp_path / "scratch",
        )


def test_online_snapshot_cannot_be_migration_baseline(tmp_path: Path) -> None:
    client = InventoryClient([row("a", 1)], 1)
    with pytest.raises(ValueError, match="stopped"):
        scan_inventory(
            client,
            str(tmp_path / "source"),
            expected_uri="https://1.invalid",
            expected_id="1",
            frozen=False,
            workspace=tmp_path / "scratch",
        )


class RepeatedCountClient(InventoryClient):
    """Count includes old repeated physical IDs; queries return their logical rows."""

    def query(self, name: str, **kwargs: Any) -> list[dict[str, int]]:
        assert name == "arxiv_papers" and kwargs["output_fields"] == ["count(*)"]
        expression = kwargs.get("filter", "")
        if expression:
            pk = json.loads(expression.split(" == ", 1)[1])
            return [{"count(*)": 2 if pk == "a" else 1}]
        return [{"count(*)": len(self.rows) + 1}]

    def get(self, name: str, *, ids: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        assert name == "arxiv_papers"
        assert kwargs["output_fields"] == ["arxiv_id", "version", "updated", "created"]
        return [r for r in self.rows if r["arxiv_id"] in ids]


def test_count_difference_needs_explicit_per_key_evidence(tmp_path: Path) -> None:
    source = RepeatedCountClient([row("a", 1), row("b", 1)], 1)
    with pytest.raises(ValueError, match="count"):
        inventory(source, tmp_path / "source")
    result = inventory(source, tmp_path / "source", count_duplicate_ids=("a",))
    assert result["rows"] == 2 and result["expected_rows"] == 3 and result["complete"]
    assert result["count_duplicates"] == [
        {"arxiv_id": "a", "physical_count": 2, "scalar_sha256": digest_json(row("a", 1))}
    ]


def test_unproven_count_exception_cannot_hide_a_missing_paper(tmp_path: Path) -> None:
    source = RepeatedCountClient([row("a", 1), row("b", 1)], 1)
    with pytest.raises(ValueError, match="duplicate"):
        inventory(source, tmp_path / "source", count_duplicate_ids=("b",))


def test_count_evidence_requires_the_same_visible_scalar_record(tmp_path: Path) -> None:
    source = RepeatedCountClient([row("a", 1)], 1)
    with patch.object(source, "get", return_value=[row("a", 2)]):
        with pytest.raises(ValueError, match="changed"):
            inventory(source, tmp_path / "source", count_duplicate_ids=("a",))


def test_repeated_count_ids_cannot_become_automatic_delta_writes(tmp_path: Path) -> None:
    source = RepeatedCountClient([row("a", 2)], 1)
    target = InventoryClient([row("a", 1)], 2)
    inventory(source, tmp_path / "source", count_duplicate_ids=("a",))
    inventory(target, tmp_path / "target")
    with pytest.raises(ValueError, match="duplicate"):
        build_delta(
            str(tmp_path / "source"),
            str(tmp_path / "target"),
            str(tmp_path / "plan"),
            workspace=tmp_path / "scratch",
        )


def test_repeated_iterator_keys_are_still_rejected_with_count_evidence(tmp_path: Path) -> None:
    source = RepeatedCountClient([row("a", 1), row("a", 1)], 1)
    with pytest.raises(ValueError, match="Duplicate"):
        inventory(source, tmp_path / "source", count_duplicate_ids=("a",))


@pytest.mark.parametrize(
    "proof", [None, 3, {"arxiv_id": "a", "physical_count": 2, "scalar_sha256": 3}]
)
def test_malformed_count_proofs_fail_closed(proof: Any) -> None:
    with pytest.raises(ValueError, match="evidence"):
        checked_proofs({"count_duplicates": [proof]})


def test_a_valid_duplicate_proof_cannot_explain_an_extra_missing_row(tmp_path: Path) -> None:
    source = RepeatedCountClient([row("a", 1), row("b", 1)], 1)
    query = source.query

    def more_count(name: str, **kwargs: Any) -> list[dict[str, int]]:
        return query(name, **kwargs) if kwargs.get("filter") else [{"count(*)": 4}]

    with patch.object(source, "query", more_count), pytest.raises(ValueError, match="Unexplained"):
        inventory(source, tmp_path / "source", count_duplicate_ids=("a",))


def test_duplicate_evidence_must_remain_bound_on_resume(tmp_path: Path) -> None:
    source = RepeatedCountClient([row("a", 1)], 1)
    inventory(source, tmp_path / "source", count_duplicate_ids=("a",))
    with pytest.raises(ValueError, match="changed during resume"):
        inventory(source, tmp_path / "source")


def test_corrupted_scalar_evidence_cannot_be_used_for_a_delta(tmp_path: Path) -> None:
    source = RepeatedCountClient([row("a", 1)], 1)
    inventory(source, tmp_path / "source", count_duplicate_ids=("a",))
    manifest_path = tmp_path / "source" / "manifest.json"
    saved = json.loads(manifest_path.read_text())
    saved["count_duplicates"][0]["scalar_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(saved))
    inventory(InventoryClient([row("a", 1)], 2), tmp_path / "target")
    with pytest.raises(ValueError, match="does not match"):
        build_delta(
            str(tmp_path / "source"),
            str(tmp_path / "target"),
            str(tmp_path / "plan"),
            workspace=tmp_path / "scratch",
        )
