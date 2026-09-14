"""Migration resumes a pinned launch and never certifies a failed ECS task."""

import copy
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "personal_migration", ROOT / "scripts/personal_migration.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def definition():
    return {
        "family": "scholight-personal-migration",
        "networkMode": "bridge",
        "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
        "taskRoleArn": "arn:aws:iam::669409472143:role/ScholightPersonalMigrationTask",
        "executionRoleArn": "arn:aws:iam::669409472143:role/ScholightPersonalMigrationExecution",
        "containerDefinitions": [
            {
                "name": "migration",
                "image": "old",
                "secrets": [{"name": "DB", "valueFrom": "private"}],
            }
        ],
    }


def test_migration_changes_only_candidate_image_and_command():
    original = definition()
    result = m.candidate_definition(original, {"images": {"api": "new"}})
    assert result["containerDefinitions"][0]["image"] == "new"
    assert (
        result["containerDefinitions"][0]["secrets"]
        == original["containerDefinitions"][0]["secrets"]
    )
    assert original["containerDefinitions"][0]["image"] == "old"
    with pytest.raises(ValueError):
        m.candidate_definition(
            original | {"taskRoleArn": "administrator"}, {"images": {"api": "new"}}
        )


@pytest.mark.parametrize("exit_code", [0, 1])
def test_migration_receipt_requires_success_and_retry_does_not_duplicate_work(
    monkeypatch, exit_code
):
    import personal_apply

    monkeypatch.setattr(personal_apply, "compatible_with_running", lambda *args: True)
    manifest = {
        "images": {"api": "new"},
        "identity_revision": "a" * 40,
        "migrations": {"016": "b" * 64},
    }

    class Release:
        def __init__(self):
            self.records, self.objects = {}, {}
            self.s3 = self.ecs = self
            self.launches = self.registrations = 0
            self.paused = False

        def stack(self, name):
            return {
                "StackStatus": "UPDATE_COMPLETE",
                "Parameters": [
                    {"ParameterKey": "DatabaseMigratorSecretArn", "ParameterValue": "private"},
                    {"ParameterKey": "MetadataEnabled", "ParameterValue": "true"},
                ],
                "Outputs": [{"OutputKey": "MigrationTaskDefinitionArn", "OutputValue": "taskdef"}],
            }

        def read(self, name):
            return self.records.get(name)

        def record(self, name, value):
            self.records[name] = copy.deepcopy(value)

        def change_background(self, stage, overrides):
            assert overrides == {"MetadataEnabled": "false"}
            self.paused = True

        def wait_ack(self):
            assert self.paused

        def wait_drained(self):
            assert self.paused

        def get_object(self, **kwargs):
            if kwargs["Key"] not in self.objects:
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}

        def put_object(self, **kwargs):
            assert kwargs["IfNoneMatch"] == "*"
            self.objects[kwargs["Key"]] = kwargs["Body"]

        def describe_task_definition(self, **kwargs):
            return {"taskDefinition": definition()}

        def register_task_definition(self, **kwargs):
            self.registrations += 1
            return {"taskDefinition": {"taskDefinitionArn": "candidate"}}

        def run_task(self, **kwargs):
            self.launches += 1
            assert self.read("migration-launch")["definition"] == "candidate"
            return {"tasks": [{"taskArn": "task"}]}

        def describe_tasks(self, **kwargs):
            return {"tasks": [{"lastStatus": "STOPPED", "containers": [{"exitCode": exit_code}]}]}

        def deregister_task_definition(self, **kwargs):
            pass

    release = Release()
    monkeypatch.setattr(m, "AdmissionRelease", lambda *args: release)
    for _ in range(2):
        if exit_code:
            with pytest.raises(RuntimeError, match="Migration failed"):
                m.execute(manifest, "c" * 64)
        else:
            m.execute(manifest, "c" * 64)
    assert release.launches == release.registrations == 1
    assert release.paused
    if exit_code:
        assert not release.objects
    else:
        assert json.loads(next(iter(release.objects.values())))["status"] == "complete"
