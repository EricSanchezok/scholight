"""Lossless archive boundaries, with no remote dependencies."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from scholight.store.archive import export_archive, restore_archive, verify_archive, verify_restored


class IteratorStub:
    def __init__(self, rows: list[dict[str, Any]], checkpoint: str) -> None:
        self.rows = rows
        self.path = Path(checkpoint)
        self.position = int(self.path.read_text()) if self.path.exists() else 0

    def next(self) -> list[dict[str, Any]]:
        result = self.rows[self.position : self.position + 2]
        self.position += len(result)
        self.path.write_text(str(self.position))
        return deepcopy(result)

    def close(self) -> None:
        pass


class ClientStub:
    def __init__(self, rows: list[dict[str, Any]] | None = None, identity: int = 1) -> None:
        self.rows = rows or []
        self.identity = identity
        self.reads = 0

    def describe_collection(self, name: str) -> dict[str, Any]:
        return {
            "collection_name": name,
            "collection_id": self.identity,
            "fields": [
                {"name": "arxiv_id", "type": 21, "is_primary": True, "params": {"max_length": 32}},
                {"name": "abstract", "type": 21, "params": {"max_length": 16384}},
                {"name": "abstract_embedding", "type": 101, "params": {"dim": 2}},
            ],
            "functions": [],
            "enable_dynamic_field": False,
        }

    def list_indexes(self, _name: str) -> list[str]:
        return []

    def query(self, _name: str, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{"count(*)": len(self.rows)}]

    def query_iterator(self, _name: str, **kwargs: Any) -> IteratorStub:
        self.reads += 1
        return IteratorStub(self.rows, kwargs["iterator_cp_file"])

    def upsert(self, _name: str, *, data: list[dict[str, Any]], **_kwargs: Any) -> dict[str, int]:
        keyed = {row["arxiv_id"]: row for row in self.rows}
        keyed.update({row["arxiv_id"]: row for row in deepcopy(data)})
        self.rows = list(keyed.values())
        return {"upsert_count": len(data)}


@pytest.fixture
def source() -> ClientStub:
    return ClientStub(
        [
            {"arxiv_id": name, "abstract": "Text " + name, "abstract_embedding": [0.25, -0.5]}
            for name in ["2401.00003", "2401.00001", "2401.00002"]
        ]
    )


def test_roundtrip_unordered_iterator_without_embedding(source: ClientStub, tmp_path: Path) -> None:
    destination = str(tmp_path / "archive")
    export_archive(
        source,
        "arxiv_papers",
        destination,
        source_uri="https://source.invalid",
        frozen=True,
        shard_bytes=1,
    )
    target = ClientStub(identity=2)
    restore_archive(target, "arxiv_papers", destination, target_uri="https://target.invalid")
    assert target.rows == source.rows


def test_corrupt_shard_fails_before_target_write(source: ClientStub, tmp_path: Path) -> None:
    destination = str(tmp_path / "archive")
    export_archive(
        source, "arxiv_papers", destination, source_uri="https://source.invalid", frozen=True
    )
    next((tmp_path / "archive").glob("*.parquet")).write_bytes(b"broken")
    target = ClientStub(identity=2)
    with pytest.raises(ValueError, match="checksum"):
        restore_archive(target, "arxiv_papers", destination, target_uri="https://target.invalid")
    assert target.rows == []


def test_duplicate_primary_keys_rejected(source: ClientStub, tmp_path: Path) -> None:
    source.rows.append(deepcopy(source.rows[0]))
    destination = str(tmp_path / "archive")
    with pytest.raises(ValueError, match="Duplicate"):
        export_archive(
            source, "arxiv_papers", destination, source_uri="https://source.invalid", frozen=True
        )


def test_online_archive_is_not_final(source: ClientStub, tmp_path: Path) -> None:
    destination = str(tmp_path / "archive")
    export_archive(
        source, "arxiv_papers", destination, source_uri="https://source.invalid", frozen=False
    )
    assert verify_archive(destination)["final_archive"] is False


def test_source_target_identity_cannot_match(source: ClientStub, tmp_path: Path) -> None:
    destination = str(tmp_path / "archive")
    export_archive(
        source, "arxiv_papers", destination, source_uri="https://source.invalid", frozen=True
    )
    with pytest.raises(ValueError, match="source"):
        restore_archive(source, "arxiv_papers", destination, target_uri="https://source.invalid")


def test_restore_verification_checks_every_value(source: ClientStub, tmp_path: Path) -> None:
    destination = str(tmp_path / "archive")
    export_archive(
        source, "arxiv_papers", destination, source_uri="https://source.invalid", frozen=True
    )
    target = ClientStub(identity=2)
    restore_archive(target, "arxiv_papers", destination, target_uri="https://target.invalid")
    target.rows[-1]["abstract"] = "altered"
    with pytest.raises(ValueError, match="differs"):
        verify_restored(target, "arxiv_papers", destination, target_uri="https://target.invalid")


def test_resume_after_upload_failure_uses_committed_checkpoint(
    source: ClientStub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scholight.store.archive_io import ArchiveLocation

    destination = str(tmp_path / "archive")
    original = ArchiveLocation.upload
    uploaded = 0

    def upload(location: ArchiveLocation, path: Path, name: str) -> None:
        nonlocal uploaded
        if name.endswith(".parquet"):
            uploaded += 1
            if uploaded == 2:
                raise OSError("upload interrupted")
        original(location, path, name)

    monkeypatch.setattr(ArchiveLocation, "upload", upload)
    with pytest.raises(OSError, match="interrupted"):
        export_archive(
            source,
            "arxiv_papers",
            destination,
            source_uri="https://source.invalid",
            frozen=True,
            shard_bytes=1,
        )
    with pytest.raises(ValueError, match="incomplete"):
        verify_archive(destination)
    result = export_archive(
        source,
        "arxiv_papers",
        destination,
        source_uri="https://source.invalid",
        frozen=True,
        shard_bytes=1,
    )
    assert result["rows"] == 3


def test_resume_after_target_write_before_checkpoint_is_idempotent(
    source: ClientStub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scholight.store.archive_io import ArchiveLocation

    destination = str(tmp_path / "archive")
    export_archive(
        source,
        "arxiv_papers",
        destination,
        source_uri="https://source.invalid",
        frozen=True,
        shard_bytes=1,
    )
    target = ClientStub(identity=2)
    original = ArchiveLocation.write_json
    failed = False

    def write(
        location: ArchiveLocation, name: str, payload: dict[str, Any], workspace: Path
    ) -> None:
        nonlocal failed
        if name.startswith("restore-") and payload["shards_done"] == 1 and not failed:
            failed = True
            raise OSError("checkpoint interrupted")
        original(location, name, payload, workspace)

    monkeypatch.setattr(ArchiveLocation, "write_json", write)
    with pytest.raises(OSError, match="interrupted"):
        restore_archive(target, "arxiv_papers", destination, target_uri="https://target.invalid")
    result = restore_archive(
        target, "arxiv_papers", destination, target_uri="https://target.invalid"
    )
    assert result["restoration_verified"] and len(target.rows) == 3


@pytest.mark.parametrize("scenario", ["nonempty", "wrong_collection", "source_alias", "dimension"])
def test_wrong_target_rejected_before_write(
    source: ClientStub, tmp_path: Path, scenario: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = str(tmp_path / "archive")
    export_archive(
        source, "arxiv_papers", destination, source_uri="https://source.invalid", frozen=True
    )
    target = ClientStub(identity=1 if scenario == "source_alias" else 2)
    if scenario == "nonempty":
        target.rows = deepcopy(source.rows[:1])
    if scenario == "dimension":
        schema = target.describe_collection("arxiv_papers")
        schema["fields"][-1]["params"]["dim"] = 3
        monkeypatch.setattr(target, "describe_collection", lambda _: schema)
    before = deepcopy(target.rows)
    with pytest.raises(ValueError):
        restore_archive(
            target,
            "arxiv_chunks" if scenario == "wrong_collection" else "arxiv_papers",
            destination,
            target_uri="https://target.invalid",
        )
    assert target.rows == before


def test_concurrent_manifest_writer_cannot_overwrite_progress(tmp_path: Path) -> None:
    from scholight.store.archive_io import ArchiveLocation

    first = ArchiveLocation(str(tmp_path / "archive"))
    second = ArchiveLocation(str(tmp_path / "archive"))
    first.write_json("manifest.json", {"rows": 1}, tmp_path)
    second.read_json("manifest.json")
    first.write_json("manifest.json", {"rows": 2}, tmp_path)
    with pytest.raises(ValueError, match="Concurrent"):
        second.write_json("manifest.json", {"rows": 3}, tmp_path)
    assert first.read_json("manifest.json")["rows"] == 2


def test_scalar_schema_checked_before_export_commit(source: ClientStub, tmp_path: Path) -> None:
    source.rows[-1]["abstract"] = 42
    with pytest.raises(ValueError, match="scalar"):
        export_archive(
            source,
            "arxiv_papers",
            str(tmp_path / "archive"),
            source_uri="https://source.invalid",
            frozen=True,
        )


def test_archive_records_code_provenance(source: ClientStub, tmp_path: Path) -> None:
    import json

    destination = str(tmp_path / "archive")
    export_archive(
        source, "arxiv_papers", destination, source_uri="https://source.invalid", frozen=True
    )
    manifest = json.loads((Path(destination) / "manifest.json").read_text())
    assert len(manifest["code_sha256"]) == 64


def test_legacy_corrupt_tail_fails_before_any_upsert(tmp_path: Path) -> None:
    import gzip

    from scholight.store.export import restore_collection_from_path

    with gzip.open(tmp_path / "shard_0000.jsonl.gz", "wt") as stream:
        stream.write('{"arxiv_id":"2401.00001"}\nnot-json\n')
    target = ClientStub(identity=2)
    with pytest.raises(ValueError, match="Corrupt"):
        restore_collection_from_path(target, "arxiv_papers", tmp_path, batch_size=1)
    assert target.rows == []
