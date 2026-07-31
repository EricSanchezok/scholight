"""Survey artifact safety and ordering tests."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from scholight.survey.artifacts import SurveyArtifactError, SurveyArtifactStore


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.operations: list[tuple[str, str]] = []

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

    def get_object(self, **kwargs: Any) -> dict[str, io.BytesIO]:
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}

    def generate_presigned_url(
        self,
        operation: str,
        **kwargs: Any,
    ) -> str:
        del operation
        return f"https://signed.invalid/{kwargs['Params']['Key']}?expires={kwargs['ExpiresIn']}"

    def delete_objects(self, **kwargs: Any) -> dict[str, list[object]]:
        for item in kwargs["Delete"]["Objects"]:
            self.objects.pop(item["Key"], None)
        return {"Errors": []}

    def delete_object(self, **kwargs: Any) -> None:
        self.objects.pop(kwargs["Key"], None)


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
