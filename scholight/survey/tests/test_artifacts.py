"""Survey artifact safety and ordering tests."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from scholight.survey.artifacts import SurveyArtifactError, SurveyArtifactStore


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
