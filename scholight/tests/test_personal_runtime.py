"""Personal deployment isolates admitted fulltext work and never inherits source accounts."""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "personal", Path(__file__).resolve().parents[2] / "scripts/personal_runtime.py"
)
assert spec and spec.loader
personal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(personal)


def test_runtime_contains_three_services_and_independently_admitted_ingest() -> None:
    template = personal.runtime()
    resources = template["Resources"]
    services = [r for r in resources.values() if r["Type"] == "AWS::ECS::Service"]
    assert len(services) == 3
    api = resources["ApiTask"]["Properties"]["ContainerDefinitions"][0]
    sync = resources["MetadataTask"]["Properties"]["ContainerDefinitions"][0]
    assert {e["Name"]: e["Value"] for e in api["Environment"]}["SCHOLIGHT_RUNTIME_PROFILE"] == {
        "Ref": "RuntimeProfile"
    }
    assert template["Parameters"]["RuntimeProfile"]["Default"] == "lean"
    assert "SearchApiSecretArn" in json.dumps(api["Secrets"])
    assert "SearchSyncSecretArn" in json.dumps(sync["Secrets"])
    assert "SearchSyncSecretArn" not in json.dumps(api)
    assert "DEEPSEEK_API_KEY" not in json.dumps(template)
    assert not any("Survey" in name for name in resources)
    assert "IngestTask" in resources and "IngestService" not in resources
    assert template["Parameters"]["IngestEnabled"]["Default"] == "false"


def test_foundation_preserves_secrets_and_uses_manual_personal_oidc() -> None:
    template = personal.foundation()
    for resource in template["Resources"].values():
        if resource["Type"] == "AWS::SecretsManager::Secret":
            assert resource["DeletionPolicy"] == "RetainExceptOnCreate"
    text = json.dumps(template)
    assert ":environment:personal-image-publish" in text
    assert "919651863140" not in text
    assert "d432d46d6c77308" not in text


def test_metadata_batch_tuning_keeps_memory_and_concurrency_bounded() -> None:
    template = personal.runtime()
    parameter = template["Parameters"]["MetadataBatchSize"]
    task = template["Resources"]["MetadataTask"]["Properties"]
    env = {e["Name"]: e["Value"] for e in task["ContainerDefinitions"][0]["Environment"]}
    assert parameter == {"Type": "Number", "Default": 64, "MinValue": 1, "MaxValue": 512}
    assert env["SCHOLIGHT_METADATA_SYNC_BATCH_SIZE"] == {"Ref": "MetadataBatchSize"}
    assert env["SCHOLIGHT_EMBEDDING_CONCURRENCY"] == "1"
    assert task["Memory"] == "768"


def test_change_set_guard_rejects_foreign_stack_and_non_task_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from personal_apply import STACK, validate_changes

    change = {
        "StackName": STACK,
        "Status": "CREATE_COMPLETE",
        "Changes": [
            {
                "ResourceChange": {
                    "Action": "Modify",
                    "Replacement": "True",
                    "ResourceType": "AWS::ECS::TaskDefinition",
                }
            }
        ],
    }
    validate_changes(change)
    change["Changes"][0]["ResourceChange"]["ResourceType"] = "AWS::IAM::Role"
    with pytest.raises(ValueError, match="Only immutable"):
        validate_changes(change)
    change["Changes"][0]["ResourceChange"]["Action"] = "Remove"
    with pytest.raises(ValueError, match="removal"):
        validate_changes(change)
    change["StackName"] = "another-product"
    with pytest.raises(ValueError, match="personal runtime"):
        validate_changes(change)


def test_control_role_can_clean_up_task_revisions_without_broad_service_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from personal_control import control

    policies = control()["Resources"]["CloudFormationRole"]["Properties"]["Policies"]
    statements = policies[0]["PolicyDocument"]["Statement"]
    cleanup = next(s for s in statements if "ecs:DeregisterTaskDefinition" in s["Action"])
    # AWS authorizes deregistration against '*', not a task-definition ARN.
    assert cleanup["Resource"] == "*"
    assert cleanup["Condition"]["StringEquals"]["aws:RequestedRegion"] == {"Ref": "AWS::Region"}
    service = next(s for s in statements if "ecs:UpdateService" in s["Action"])
    assert service["Resource"] != "*"
