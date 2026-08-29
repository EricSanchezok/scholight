"""Survey artifact safety and ordering tests."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from scholight.survey import artifacts as artifacts_module
from scholight.survey.artifacts import (
    SurveyArtifactError,
    SurveyArtifactNotFoundError,
    SurveyArtifactStore,
)


class _FakeS3:
    def __init__(self, *, url_origin: str = "https://signed.invalid") -> None:
        self.objects: dict[str, bytes] = {}
        self.operations: list[tuple[str, str]] = []
        self.corrupt_get_key: str | None = None
        self.url_origin = url_origin

    def upload_fileobj(
        self,
        stream: Any,
        bucket: str,
        key: str,
        **kwargs: Any,
    ) -> None:
        del bucket, kwargs
        self.objects[key] = stream.read()
        self.operations.append(("upload_fileobj", key))

    def put_object(self, **kwargs: Any) -> None:
        if kwargs.get("IfNoneMatch") == "*" and kwargs["Key"] in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[kwargs["Key"]] = kwargs["Body"]
        self.operations.append(("put_object", kwargs["Key"]))

    def head_object(self, **kwargs: Any) -> dict[str, int]:
        return {"ContentLength": len(self.objects[kwargs["Key"]])}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        value = self.objects[kwargs["Key"]]
        body = b"corrupted" if kwargs["Key"] == self.corrupt_get_key else value
        return {"Body": io.BytesIO(body), "ContentLength": len(body)}

    def generate_presigned_url(
        self,
        operation: str,
        **kwargs: Any,
    ) -> str:
        del operation
        return f"{self.url_origin}/{kwargs['Params']['Key']}?expires={kwargs['ExpiresIn']}"

    def delete_objects(self, **kwargs: Any) -> dict[str, list[object]]:
        for item in kwargs["Delete"]["Objects"]:
            self.objects.pop(item["Key"], None)
        return {"Errors": []}

    def delete_object(self, **kwargs: Any) -> None:
        self.objects.pop(kwargs["Key"], None)

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        prefix = kwargs["Prefix"]
        return {
            "Contents": [{"Key": key} for key in sorted(self.objects) if key.startswith(prefix)],
            "IsTruncated": False,
        }


def test_s3_client_enforces_signature_v4(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    expected_client = object()

    def fake_client(service_name: str, **kwargs: Any) -> object:
        captured["service_name"] = service_name
        captured.update(kwargs)
        return expected_client

    monkeypatch.setattr("scholight.survey.artifacts.boto3.client", fake_client)

    client = artifacts_module._s3_client(None)

    assert client is expected_client
    assert captured["service_name"] == "s3"
    assert captured["config"].signature_version == "s3v4"


def _install_manifest_v2_overlay(
    *,
    fake: _FakeS3,
    archive: Any,
    report: bytes = b"# Recovered Survey",
    index: bytes = b"# Recovered Survey\n\n[Open report](08_survey.md)",
) -> str:
    report_sha256 = hashlib.sha256(report).hexdigest()
    recovery_prefix = f"{archive.storage_prefix}/recoveries/{report_sha256}"
    records = []
    for path, content in (
        ("run/08_survey.md", report),
        ("run/index.md", index),
    ):
        key = f"{recovery_prefix}/{path}"
        fake.objects[key] = content
        records.append(
            {
                "path": path,
                "key": key,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "mime": "text/markdown",
            }
        )
    parent_body = fake.objects[archive.manifest_key]
    manifest = {
        "schema_version": 2,
        "job_id": archive.manifest["job_id"],
        "user_id": archive.manifest["user_id"],
        "parent_manifest": {
            "key": archive.manifest_key,
            "sha256": hashlib.sha256(parent_body).hexdigest(),
        },
        "files": records,
    }
    manifest_key = f"{recovery_prefix}/manifest.json"
    fake.objects[manifest_key] = json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return manifest_key


@pytest.mark.asyncio
async def test_archive_uploads_complete_tree_and_manifest_last(tmp_path: Path) -> None:
    (tmp_path / "08_survey.md").write_text("# Survey", encoding="utf-8")
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "paper.md").write_text("Evidence", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)

    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=tmp_path,
        run_metadata={"outcome": "succeeded"},
    )

    assert [record["path"] for record in archive.manifest["files"]] == [
        "run/08_survey.md",
        "run/cards/paper.md",
        "run.json",
    ]
    assert archive.manifest["files"][0]["mime"] == "text/markdown"
    assert fake.operations[-1] == ("put_object", archive.manifest_key)


@pytest.mark.asyncio
async def test_archive_does_not_follow_symbolic_links(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("do not upload", encoding="utf-8")
    (tmp_path / "safe.txt").write_text("safe", encoding="utf-8")
    (tmp_path / "unsafe-link").symlink_to(outside)
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)

    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=tmp_path,
        run_metadata={"outcome": "failed"},
    )

    assert archive.manifest["excluded_unsafe_paths"] == ["unsafe-link"]
    assert all("unsafe-link" not in key for key in fake.objects)


@pytest.mark.asyncio
async def test_delete_uses_only_manifest_keys(tmp_path: Path) -> None:
    (tmp_path / "08_survey.md").write_text("# Survey", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=tmp_path,
        run_metadata={"outcome": "succeeded"},
    )
    unrelated_key = "surveys/v1/another-owner/file"
    fake.objects[unrelated_key] = json.dumps({"keep": True}).encode()

    await store.delete_archive(manifest_key=archive.manifest_key)

    assert fake.objects == {unrelated_key: b'{"keep": true}'}


@pytest.mark.asyncio
async def test_delete_can_preserve_manifest_until_database_row_is_removed(tmp_path: Path) -> None:
    (tmp_path / "08_survey.md").write_text("# Survey", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=tmp_path,
        run_metadata={"outcome": "succeeded"},
    )

    await store.delete_archive(manifest_key=archive.manifest_key, preserve_manifest=True)

    assert list(fake.objects) == [archive.manifest_key]
    await store.delete_manifest(manifest_key=archive.manifest_key)
    assert fake.objects == {}


@pytest.mark.asyncio
async def test_manifest_v2_overlays_report_without_hiding_v1_artifacts(tmp_path: Path) -> None:
    (tmp_path / "00_outline.md").write_text("## Title\nOriginal", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=tmp_path,
        run_metadata={"outcome": "failed"},
    )
    manifest_key = _install_manifest_v2_overlay(fake=fake, archive=archive)

    artifacts = await store.presigned_artifacts(manifest_key=manifest_key)
    report = await store.open_artifact(
        manifest_key=manifest_key,
        path="run/08_survey.md",
    )

    assert {artifact["path"] for artifact in artifacts} == {
        "run/00_outline.md",
        "run.json",
        "run/08_survey.md",
        "run/index.md",
    }
    assert b"".join([chunk async for chunk in report.chunks()]) == b"# Recovered Survey"

    package = await store.build_report_package(manifest_key=manifest_key)
    package_body = b"".join([chunk async for chunk in package.chunks()])
    with zipfile.ZipFile(io.BytesIO(package_body)) as report_zip:
        assert report_zip.read("08_survey.md") == b"# Recovered Survey"


@pytest.mark.asyncio
async def test_manifest_v2_rejects_parent_checksum_mismatch(tmp_path: Path) -> None:
    (tmp_path / "00_outline.md").write_text("## Title\nOriginal", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=tmp_path,
        run_metadata={"outcome": "failed"},
    )
    manifest_key = _install_manifest_v2_overlay(fake=fake, archive=archive)
    manifest = json.loads(fake.objects[manifest_key])
    manifest["parent_manifest"]["sha256"] = "0" * 64
    fake.objects[manifest_key] = json.dumps(manifest).encode()

    with pytest.raises(SurveyArtifactError, match="parent checksum"):
        await store.presigned_artifacts(manifest_key=manifest_key)


@pytest.mark.asyncio
async def test_manifest_v2_rejects_a_circular_parent_manifest(tmp_path: Path) -> None:
    (tmp_path / "00_outline.md").write_text("## Title\nOriginal", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=tmp_path,
        run_metadata={"outcome": "failed"},
    )
    manifest_key = _install_manifest_v2_overlay(fake=fake, archive=archive)
    parent = json.loads(fake.objects[archive.manifest_key])
    parent["schema_version"] = 2
    parent["parent_manifest"] = {
        "key": archive.manifest_key,
        "sha256": "0" * 64,
    }
    parent_body = json.dumps(parent).encode()
    fake.objects[archive.manifest_key] = parent_body
    overlay = json.loads(fake.objects[manifest_key])
    overlay["parent_manifest"]["sha256"] = hashlib.sha256(parent_body).hexdigest()
    fake.objects[manifest_key] = json.dumps(overlay).encode()

    with pytest.raises(SurveyArtifactError, match="parent manifest"):
        await store.presigned_artifacts(manifest_key=manifest_key)


@pytest.mark.asyncio
async def test_manifest_v2_rejects_unsafe_or_non_report_overrides(tmp_path: Path) -> None:
    (tmp_path / "00_outline.md").write_text("## Title\nOriginal", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=tmp_path,
        run_metadata={"outcome": "failed"},
    )
    manifest_key = _install_manifest_v2_overlay(fake=fake, archive=archive)
    manifest = json.loads(fake.objects[manifest_key])
    manifest["files"][0]["path"] = "run/cards/../../secret.md"
    fake.objects[manifest_key] = json.dumps(manifest).encode()

    with pytest.raises(SurveyArtifactError, match="overlay entry"):
        await store.presigned_artifacts(manifest_key=manifest_key)


@pytest.mark.asyncio
async def test_manifest_v2_cleanup_deletes_overlay_and_parent_only(tmp_path: Path) -> None:
    (tmp_path / "00_outline.md").write_text("## Title\nOriginal", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    job_id = uuid4()
    archive = await store.archive_run(
        user_id=42,
        job_id=job_id,
        run_root=tmp_path,
        run_metadata={"outcome": "failed"},
    )
    manifest_key = _install_manifest_v2_overlay(fake=fake, archive=archive)
    unrelated_key = f"surveys/v1/42/{uuid4()}/run/keep.md"
    fake.objects[unrelated_key] = b"keep"

    await store.cleanup_archive(
        user_id=42,
        job_id=job_id,
        storage_prefix=archive.storage_prefix,
        manifest_key=manifest_key,
    )

    assert fake.objects == {unrelated_key: b"keep"}


@pytest.mark.asyncio
async def test_manifest_v2_preserved_manifests_are_deleted_together(tmp_path: Path) -> None:
    (tmp_path / "00_outline.md").write_text("## Title\nOriginal", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=tmp_path,
        run_metadata={"outcome": "failed"},
    )
    manifest_key = _install_manifest_v2_overlay(fake=fake, archive=archive)

    await store.delete_archive(manifest_key=manifest_key, preserve_manifest=True)

    assert set(fake.objects) == {archive.manifest_key, manifest_key}
    await store.delete_manifest(manifest_key=manifest_key)
    assert fake.objects == {}


@pytest.mark.asyncio
async def test_recovery_overlay_writer_is_append_only_and_readable(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "00_outline.md").write_text("## Title\nRecovered", encoding="utf-8")
    recovered = tmp_path / "recovered"
    recovered.mkdir()
    report_path = recovered / "08_survey.md"
    index_path = recovered / "index.md"
    report_path.write_text("# Recovered Survey\n", encoding="utf-8")
    index_path.write_text("# Index\n", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=source,
        run_metadata={"outcome": "failed"},
    )
    source_sha256 = hashlib.sha256(fake.objects[archive.manifest_key]).hexdigest()

    overlay = await store.create_recovery_overlay(
        source_manifest_key=archive.manifest_key,
        expected_source_sha256=source_sha256,
        report_path=report_path,
        index_path=index_path,
    )
    repeated = await store.create_recovery_overlay(
        source_manifest_key=archive.manifest_key,
        expected_source_sha256=source_sha256,
        report_path=report_path,
        index_path=index_path,
    )
    report = await store.open_artifact(
        manifest_key=overlay.manifest_key,
        path="run/08_survey.md",
    )

    assert overlay == repeated
    assert overlay.manifest["schema_version"] == 2
    assert overlay.source_manifest_sha256 == source_sha256
    assert b"".join([chunk async for chunk in report.chunks()]) == b"# Recovered Survey\n"


@pytest.mark.asyncio
async def test_recovery_overlay_writer_rejects_source_hash_change(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "00_outline.md").write_text("## Title\nRecovered", encoding="utf-8")
    recovered = tmp_path / "recovered"
    recovered.mkdir()
    report_path = recovered / "08_survey.md"
    index_path = recovered / "index.md"
    report_path.write_text("# Recovered Survey\n", encoding="utf-8")
    index_path.write_text("# Index\n", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=source,
        run_metadata={"outcome": "failed"},
    )

    with pytest.raises(SurveyArtifactError, match="source checksum"):
        await store.create_recovery_overlay(
            source_manifest_key=archive.manifest_key,
            expected_source_sha256="0" * 64,
            report_path=report_path,
            index_path=index_path,
        )


@pytest.mark.asyncio
async def test_evidence_repair_overlay_replaces_only_cards_report_and_index(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "00_outline.md").write_text("## Original outline\n", encoding="utf-8")
    (source / "08_survey.md").write_text("# Original report\n", encoding="utf-8")
    (source / "index.md").write_text("# Original index\n", encoding="utf-8")
    (source / "cards").mkdir()
    (source / "cards" / "2601.21473.md").write_text(
        "# Original card without evidence\n",
        encoding="utf-8",
    )
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=source,
        run_metadata={"outcome": "succeeded"},
    )
    source_sha256 = hashlib.sha256(fake.objects[archive.manifest_key]).hexdigest()

    repaired = tmp_path / "repaired"
    repaired.mkdir()
    (repaired / "08_survey.md").write_text("# Repaired report\n", encoding="utf-8")
    (repaired / "index.md").write_text("# Repaired index\n", encoding="utf-8")
    (repaired / "cards").mkdir()
    (repaired / "cards" / "2601.21473.md").write_text(
        "# Repaired card\n\n## evidence\n- level: full_text\n- reason: pdf_text_extracted\n",
        encoding="utf-8",
    )

    overlay = await store.create_evidence_repair_overlay(
        source_manifest_key=archive.manifest_key,
        expected_source_sha256=source_sha256,
        run_root=repaired,
        repaired_cards=("cards/2601.21473.md",),
    )
    repeated = await store.create_evidence_repair_overlay(
        source_manifest_key=archive.manifest_key,
        expected_source_sha256=source_sha256,
        run_root=repaired,
        repaired_cards=("cards/2601.21473.md",),
    )
    card = await store.open_artifact(
        manifest_key=overlay.manifest_key,
        path="run/cards/2601.21473.md",
    )
    outline = await store.open_artifact(
        manifest_key=overlay.manifest_key,
        path="run/00_outline.md",
    )

    assert overlay == repeated
    assert overlay.manifest["schema_version"] == 3
    assert overlay.manifest["repair_type"] == "evidence_declarations"
    assert b"Repaired card" in b"".join([chunk async for chunk in card.chunks()])
    assert b"Original outline" in b"".join([chunk async for chunk in outline.chunks()])

    await store.delete_archive(manifest_key=overlay.manifest_key)

    assert fake.objects == {}


@pytest.mark.asyncio
async def test_evidence_repair_overlay_rejects_unbounded_or_unsafe_card_paths(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "08_survey.md").write_text("# Original report\n", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=source,
        run_metadata={"outcome": "succeeded"},
    )
    source_sha256 = hashlib.sha256(fake.objects[archive.manifest_key]).hexdigest()

    with pytest.raises(SurveyArtifactError, match="card path"):
        await store.create_evidence_repair_overlay(
            source_manifest_key=archive.manifest_key,
            expected_source_sha256=source_sha256,
            run_root=source,
            repaired_cards=("cards/../secret.md",),
        )


@pytest.mark.asyncio
async def test_manifest_cannot_presign_another_owner_key(tmp_path: Path) -> None:
    (tmp_path / "08_survey.md").write_text("# Survey", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=tmp_path,
        run_metadata={"outcome": "succeeded"},
    )
    manifest = json.loads(fake.objects[archive.manifest_key])
    manifest["files"][0]["key"] = "surveys/v1/99/foreign/run/secret.txt"
    fake.objects[archive.manifest_key] = json.dumps(manifest).encode()

    with pytest.raises(SurveyArtifactError, match="entry"):
        await store.presigned_artifacts(manifest_key=archive.manifest_key)


@pytest.mark.asyncio
async def test_presigned_artifacts_use_browser_facing_client(tmp_path: Path) -> None:
    (tmp_path / "08_global_picture.png").write_bytes(b"image")
    storage = _FakeS3(url_origin="http://minio:9000")
    browser = _FakeS3(url_origin="http://127.0.0.1:9000")
    store = SurveyArtifactStore(
        bucket="survey-test",
        client=storage,
        presign_client=browser,
    )
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=tmp_path,
        run_metadata={"outcome": "succeeded"},
    )

    artifacts = await store.presigned_artifacts(manifest_key=archive.manifest_key)

    assert artifacts[0]["url"].startswith("http://127.0.0.1:9000/")


@pytest.mark.asyncio
async def test_manifest_key_must_match_its_declared_relative_path(tmp_path: Path) -> None:
    (tmp_path / "08_survey.md").write_text("# Survey", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=tmp_path,
        run_metadata={"outcome": "succeeded"},
    )
    manifest = json.loads(fake.objects[archive.manifest_key])
    manifest["files"][0]["key"] = f"{archive.storage_prefix}/run/../other"
    fake.objects[archive.manifest_key] = json.dumps(manifest).encode()

    with pytest.raises(SurveyArtifactError, match="entry"):
        await store.presigned_artifacts(manifest_key=archive.manifest_key)


@pytest.mark.asyncio
async def test_open_artifact_streams_only_exact_manifest_path(tmp_path: Path) -> None:
    (tmp_path / "08_survey.md").write_text("# Survey", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=tmp_path,
        run_metadata={"outcome": "succeeded"},
    )

    artifact = await store.open_artifact(
        manifest_key=archive.manifest_key,
        path="run/08_survey.md",
    )
    body = b"".join([chunk async for chunk in artifact.chunks()])

    assert body == b"# Survey"


@pytest.mark.asyncio
async def test_open_artifact_rejects_same_size_checksum_mismatch(tmp_path: Path) -> None:
    (tmp_path / "08_survey.md").write_text("# Survey", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=tmp_path,
        run_metadata={"outcome": "succeeded"},
    )
    report = next(
        record for record in archive.manifest["files"] if record["path"] == "run/08_survey.md"
    )
    fake.objects[report["key"]] = b"# Tamper"

    with pytest.raises(SurveyArtifactError, match="checksum"):
        await store.open_artifact(
            manifest_key=archive.manifest_key,
            path="run/08_survey.md",
        )


@pytest.mark.asyncio
async def test_open_artifact_rejects_path_not_in_manifest(tmp_path: Path) -> None:
    (tmp_path / "08_survey.md").write_text("# Survey", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=tmp_path,
        run_metadata={"outcome": "succeeded"},
    )

    with pytest.raises(SurveyArtifactError, match="not present"):
        await store.open_artifact(
            manifest_key=archive.manifest_key,
            path="run/../secret.txt",
        )


@pytest.mark.asyncio
async def test_recovery_restores_only_verified_contract_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "cards").mkdir()
    (source / "cards" / "cs-0012009.md").write_text("legacy card", encoding="utf-8")
    (source / "00_card_plan.json").write_text("[]", encoding="utf-8")
    (source / "paper.pdf").write_bytes(b"pdf")
    (source / "notes.txt").write_text("not a contract input", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=source,
        run_metadata={"outcome": "failed"},
    )

    restored = await store.restore_contract_workspace(
        manifest_key=archive.manifest_key,
        run_root=tmp_path / "restored",
    )

    assert set(restored) == {"00_card_plan.json", "cards/cs-0012009.md"}
    assert (tmp_path / "restored" / "cards" / "cs-0012009.md").read_text() == "legacy card"
    assert not (tmp_path / "restored" / "paper.pdf").exists()
    assert not (tmp_path / "restored" / "notes.txt").exists()


@pytest.mark.asyncio
async def test_recovery_rejects_manifest_path_traversal(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "08_survey.md").write_text("# Survey", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=source,
        run_metadata={"outcome": "failed"},
    )
    manifest = json.loads(fake.objects[archive.manifest_key])
    manifest["files"][0]["path"] = "run/../escaped.md"
    fake.objects[archive.manifest_key] = json.dumps(manifest).encode()

    with pytest.raises(SurveyArtifactError, match="entry"):
        await store.restore_contract_workspace(
            manifest_key=archive.manifest_key,
            run_root=tmp_path / "restored",
        )


@pytest.mark.asyncio
async def test_recovery_stops_when_object_exceeds_manifest_size(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "08_survey.md").write_text("# Survey", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=source,
        run_metadata={"outcome": "failed"},
    )
    report_key = next(
        record["key"]
        for record in archive.manifest["files"]
        if record["path"] == "run/08_survey.md"
    )
    fake.objects[report_key] += b"unexpected"

    with pytest.raises(SurveyArtifactError, match="does not match"):
        await store.restore_contract_workspace(
            manifest_key=archive.manifest_key,
            run_root=tmp_path / "restored",
        )


@pytest.mark.asyncio
async def test_report_package_contains_final_markdown_and_images(tmp_path: Path) -> None:
    (tmp_path / "08_survey.md").write_text("# Survey", encoding="utf-8")
    (tmp_path / "08_global_picture.png").write_bytes(b"image")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=tmp_path,
        run_metadata={"outcome": "succeeded"},
    )

    package = await store.build_report_package(manifest_key=archive.manifest_key)
    body = b"".join([chunk async for chunk in package.chunks()])

    with zipfile.ZipFile(io.BytesIO(body)) as report_zip:
        assert sorted(report_zip.namelist()) == [
            "08_global_picture.png",
            "08_survey.md",
            "manifest.json",
        ]


@pytest.mark.asyncio
async def test_report_package_excludes_internal_workflow_artifacts(tmp_path: Path) -> None:
    (tmp_path / "08_survey.md").write_text("# Survey", encoding="utf-8")
    (tmp_path / "paper.pdf").write_bytes(b"pdf")
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "paper.md").write_text("Evidence", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=tmp_path,
        run_metadata={"outcome": "succeeded"},
    )

    package = await store.build_report_package(manifest_key=archive.manifest_key)
    body = b"".join([chunk async for chunk in package.chunks()])

    with zipfile.ZipFile(io.BytesIO(body)) as report_zip:
        assert "paper.pdf" not in report_zip.namelist()
        assert "cards/paper.md" not in report_zip.namelist()


@pytest.mark.asyncio
async def test_report_package_removes_internal_assembly_markers(tmp_path: Path) -> None:
    (tmp_path / "08_survey.md").write_text(
        "# Survey\n\nFinal paragraph.\n\n<!--M4-->\n",
        encoding="utf-8",
    )
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=tmp_path,
        run_metadata={"outcome": "succeeded"},
    )

    package = await store.build_report_package(manifest_key=archive.manifest_key)
    body = b"".join([chunk async for chunk in package.chunks()])

    with zipfile.ZipFile(io.BytesIO(body)) as report_zip:
        assert b"<!--M4-->" not in report_zip.read("08_survey.md")


@pytest.mark.asyncio
async def test_archive_does_not_publish_manifest_after_checksum_mismatch(tmp_path: Path) -> None:
    (tmp_path / "08_survey.md").write_text("# Survey", encoding="utf-8")
    fake = _FakeS3()
    job_id = uuid4()
    prefix = SurveyArtifactStore.prefix(user_id=42, job_id=job_id)
    fake.corrupt_get_key = f"{prefix}/run/08_survey.md"
    store = SurveyArtifactStore(bucket="survey-test", client=fake)

    with pytest.raises(SurveyArtifactError, match="checksum"):
        await store.archive_run(
            user_id=42,
            job_id=job_id,
            run_root=tmp_path,
            run_metadata={"outcome": "succeeded"},
        )

    assert f"{prefix}/manifest.json" not in fake.objects


@pytest.mark.asyncio
async def test_cleanup_missing_manifest_is_scoped_to_exact_server_prefix() -> None:
    fake = _FakeS3()
    job_id = uuid4()
    prefix = SurveyArtifactStore.prefix(user_id=42, job_id=job_id)
    fake.objects[f"{prefix}/run/partial.txt"] = b"partial"
    unrelated_key = f"surveys/v1/42/{uuid4()}/run/keep.txt"
    fake.objects[unrelated_key] = b"keep"
    store = SurveyArtifactStore(bucket="survey-test", client=fake)

    await store.cleanup_archive(
        user_id=42,
        job_id=job_id,
        storage_prefix=prefix,
        manifest_key=f"{prefix}/manifest.json",
    )

    assert fake.objects == {unrelated_key: b"keep"}


@pytest.mark.asyncio
async def test_open_report_assets_returns_markdown_and_run_relative_images(
    tmp_path: Path,
) -> None:
    (tmp_path / "08_survey.md").write_text("# Survey", encoding="utf-8")
    (tmp_path / "08_global_picture.png").write_bytes(b"image")
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "paper.md").write_text("Evidence", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=tmp_path,
        run_metadata={"outcome": "succeeded"},
    )

    report, images = await store.open_report_assets(manifest_key=archive.manifest_key)

    assert report == b"# Survey"
    assert images == {"08_global_picture.png": b"image"}


@pytest.mark.asyncio
async def test_open_report_assets_requires_final_report(tmp_path: Path) -> None:
    (tmp_path / "00_outline.md").write_text("Outline", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=tmp_path,
        run_metadata={"outcome": "failed"},
    )

    with pytest.raises(SurveyArtifactNotFoundError, match="not present"):
        await store.open_report_assets(manifest_key=archive.manifest_key)


@pytest.mark.asyncio
async def test_open_report_assets_skips_non_image_records(tmp_path: Path) -> None:
    (tmp_path / "08_survey.md").write_text("# Survey", encoding="utf-8")
    (tmp_path / "08_global_picture.png").write_bytes(b"image")
    (tmp_path / "paper.pdf").write_bytes(b"pdf")
    fake = _FakeS3()
    store = SurveyArtifactStore(bucket="survey-test", client=fake)
    archive = await store.archive_run(
        user_id=42,
        job_id=uuid4(),
        run_root=tmp_path,
        run_metadata={"outcome": "succeeded"},
    )

    report, images = await store.open_report_assets(manifest_key=archive.manifest_key)

    assert report == b"# Survey"
    assert list(images) == ["08_global_picture.png"]
