"""Content-addressed Survey workspace checkpoint contracts."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from scholight.survey.checkpoints import (
    SurveyCheckpointError,
    SurveyCheckpointStore,
)


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []

    def put_object(self, **kwargs: Any) -> dict[str, object]:
        key = str(kwargs["Key"])
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed"}},
                "PutObject",
            )
        body = kwargs["Body"]
        value = body.read() if hasattr(body, "read") else bytes(body)
        self.objects[key] = value
        self.puts.append(key)
        return {"ETag": '"etag"'}

    def get_object(self, **kwargs: Any) -> dict[str, object]:
        return {"Body": io.BytesIO(self.objects[str(kwargs["Key"])])}

    def list_objects_v2(self, **kwargs: Any) -> dict[str, object]:
        prefix = str(kwargs["Prefix"])
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        return {"Contents": [{"Key": key} for key in keys]}


@pytest.mark.asyncio
async def test_checkpoint_is_manifest_last_deduplicated_and_restorable(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "01_query_plan.md").write_text("query plan", encoding="utf-8")
    (source / "cards").mkdir()
    (source / "cards" / "paper.md").write_text("paper card", encoding="utf-8")
    (source / "diagnostics.json").write_text("private transient log", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyCheckpointStore(bucket="private", client=fake)
    job_id = uuid4()

    first = await store.publish(
        user_id=42,
        job_id=job_id,
        run_root=source,
        sequence=1,
        stage="query_plan",
        completed_units=("query_plan",),
        workflow_version="workflow-v1",
        executor_version="executor-v1",
        parent_manifest_sha256=None,
    )
    assert fake.puts[-1] == first.manifest_key
    assert [entry["path"] for entry in first.manifest["files"]] == [
        "01_query_plan.md",
        "cards/paper.md",
    ]

    second = await store.publish(
        user_id=42,
        job_id=job_id,
        run_root=source,
        sequence=2,
        stage="paper_card:paper",
        completed_units=("query_plan", "paper_card:paper"),
        workflow_version="workflow-v1",
        executor_version="executor-v1",
        parent_manifest_sha256=first.manifest_sha256,
    )
    object_puts = [key for key in fake.puts if "/objects/" in key]
    assert len(object_puts) == 2
    assert second.manifest["parent_manifest_sha256"] == first.manifest_sha256

    restored = tmp_path / "restored"
    manifest = await store.restore(
        user_id=42,
        job_id=job_id,
        run_root=restored,
        manifest_key=second.manifest_key,
        manifest_sha256=second.manifest_sha256,
    )
    assert manifest.completed_units == ("paper_card:paper", "query_plan")
    assert (restored / "01_query_plan.md").read_text(encoding="utf-8") == "query plan"
    assert (restored / "cards" / "paper.md").read_text(encoding="utf-8") == "paper card"
    assert not (restored / "diagnostics.json").exists()


@pytest.mark.asyncio
async def test_restore_rejects_manifest_tampering(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "00_survey_spec.md").write_text("scope", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyCheckpointStore(bucket="private", client=fake)
    job_id = uuid4()
    checkpoint = await store.publish(
        user_id=42,
        job_id=job_id,
        run_root=source,
        sequence=1,
        stage="anchor",
        completed_units=("anchor",),
        workflow_version="workflow-v1",
        executor_version="executor-v1",
        parent_manifest_sha256=None,
    )
    fake.objects[checkpoint.manifest_key] += b" "

    with pytest.raises(SurveyCheckpointError, match="hash"):
        await store.restore(
            user_id=42,
            job_id=job_id,
            run_root=tmp_path / "restored",
            manifest_key=checkpoint.manifest_key,
            manifest_sha256=checkpoint.manifest_sha256,
        )


@pytest.mark.asyncio
async def test_restore_rejects_path_escape_before_downloading_objects(tmp_path: Path) -> None:
    fake = _FakeS3()
    store = SurveyCheckpointStore(bucket="private", client=fake)
    job_id = uuid4()
    prefix = store.prefix(user_id=42, job_id=job_id)
    payload = json.dumps(
        {
            "schema_version": 1,
            "user_id": 42,
            "job_id": str(job_id),
            "sequence": 1,
            "stage": "query_plan",
            "workflow_version": "workflow-v1",
            "executor_version": "executor-v1",
            "parent_manifest_sha256": None,
            "completed_units": ["query_plan"],
            "files": [
                {
                    "path": "../escape.md",
                    "object_key": f"{prefix}/objects/{'a' * 64}",
                    "size": 1,
                    "sha256": "a" * 64,
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest_sha256 = hashlib.sha256(payload).hexdigest()
    manifest_key = f"{prefix}/manifests/00000001-{manifest_sha256}.json"
    fake.objects[manifest_key] = payload

    with pytest.raises(SurveyCheckpointError, match="path"):
        await store.restore(
            user_id=42,
            job_id=job_id,
            run_root=tmp_path / "restored",
            manifest_key=manifest_key,
            manifest_sha256=manifest_sha256,
        )


@pytest.mark.asyncio
async def test_uncommitted_successor_manifest_can_be_adopted_without_rerun(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "01_query_plan.md").write_text("query plan", encoding="utf-8")
    fake = _FakeS3()
    store = SurveyCheckpointStore(bucket="private", client=fake)
    job_id = uuid4()
    uploaded = await store.publish(
        user_id=42,
        job_id=job_id,
        run_root=source,
        sequence=1,
        stage="query_plan",
        completed_units=("query_plan",),
        workflow_version="workflow-v1",
        executor_version="executor-v1",
        parent_manifest_sha256=None,
    )

    successor = await store.find_successor(
        user_id=42,
        job_id=job_id,
        expected_sequence=1,
        parent_manifest_sha256=None,
        workflow_version="workflow-v1",
        executor_version="executor-v1",
    )

    assert successor is not None
    assert successor.manifest_key == uploaded.manifest_key
    assert successor.completed_units == ("query_plan",)
