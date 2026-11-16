"""Personal deployment isolates admitted fulltext work and never inherits source accounts."""

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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


def test_runtime_exposes_emf_rollback_mode_without_changing_secret_bindings() -> None:
    template = personal.runtime()
    assert template["Parameters"]["CloudWatchEmfMode"] == {
        "Type": "String",
        "Default": "critical",
        "AllowedValues": ["critical", "all"],
    }
    for resource in template["Resources"].values():
        if resource["Type"] != "AWS::ECS::TaskDefinition":
            continue
        container = resource["Properties"]["ContainerDefinitions"][0]
        environment = {e["Name"]: e["Value"] for e in container["Environment"]}
        if container["Name"] == "web":
            assert "SCHOLIGHT_CLOUDWATCH_EMF_MODE" not in environment
            continue
        assert environment["SCHOLIGHT_CLOUDWATCH_EMF_MODE"] == {"Ref": "CloudWatchEmfMode"}
        assert not any(
            s["Name"] == "SCHOLIGHT_CLOUDWATCH_EMF_MODE" for s in container.get("Secrets", [])
        )


@pytest.mark.parametrize(
    ("previous", "selection", "expected"),
    [(None, "preserve", "critical"), ("all", "preserve", "all"), ("critical", "all", "all")],
)
def test_plan_freezes_and_reports_the_selected_emf_mode(
    previous: str | None,
    selection: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    import boto3
    import personal_apply

    parameters = {
        "TargetEndpoint": "https://example.invalid",
        "IngestionTargetId": "a" * 64,
        "RuntimeProfile": "lean",
        "MetadataEnabled": "false",
        "IngestEnabled": "false",
    }
    if previous is not None:
        parameters["CloudWatchEmfMode"] = previous
    captured: dict[str, Any] = {}
    change = {
        "StackName": personal_apply.STACK,
        "Status": "CREATE_COMPLETE",
        "Changes": [
            {"ResourceChange": {"Action": "Modify", "ResourceType": "AWS::ECS::TaskDefinition"}}
        ],
    }

    def fake_aws(service: str, operation: str, *args: str) -> dict[str, Any]:
        if (service, operation) == ("sts", "get-caller-identity"):
            return {"Account": personal_apply.ACCOUNT}
        if (service, operation) == ("cloudformation", "describe-stacks"):
            return {
                "Stacks": [
                    {
                        "StackStatus": "UPDATE_COMPLETE",
                        "CreationTime": "2026-11-16T00:00:00+00:00",
                        "Parameters": [
                            {"ParameterKey": k, "ParameterValue": v} for k, v in parameters.items()
                        ],
                    }
                ]
            }
        assert (service, operation) == ("cloudformation", "create-change-set")
        path = args[args.index("--parameters") + 1].removeprefix("file://")
        captured["parameters"] = {
            p["ParameterKey"]: p["ParameterValue"] for p in json.loads(Path(path).read_text())
        }
        return {"Id": "reviewed-plan"}

    release = SimpleNamespace(
        cf=SimpleNamespace(describe_change_set=lambda **_kwargs: change),
        record=lambda _name, value: captured.update(plan=value),
    )
    monkeypatch.setenv("EXPECTED_ACCOUNT_ID", personal_apply.ACCOUNT)
    monkeypatch.setenv("AWS_REGION", personal_apply.REGION)
    monkeypatch.setenv("CLOUDFORMATION_ROLE_ARN", "reviewed-role")
    monkeypatch.setattr(
        "sys.argv", ["personal_apply.py", "plan", "--cloudwatch-emf-mode", selection]
    )
    monkeypatch.setattr(personal_apply, "aws", fake_aws)
    monkeypatch.setattr(personal_apply, "git", lambda *_args: b"a" * 40)
    monkeypatch.setattr(
        personal_apply, "read_manifest", lambda *_args: ({"source_sha": "a" * 40}, "hash")
    )
    monkeypatch.setattr(personal_apply, "compatible_with_running", lambda *_args: False)
    monkeypatch.setattr(personal_apply, "release_parameters", lambda values, *_args: dict(values))
    monkeypatch.setattr(personal_apply, "verify_versions", lambda *_args: None)
    monkeypatch.setattr(personal_apply, "AdmissionRelease", lambda *_args: release)
    monkeypatch.setattr(boto3, "client", lambda *_args, **_kwargs: object())

    personal_apply.main()

    assert captured["parameters"]["CloudWatchEmfMode"] == expected
    assert json.loads(capsys.readouterr().out)["target"]["CloudWatchEmfMode"] == expected
    assert (
        captured["plan"]["parameters_sha256"]
        == hashlib.sha256(json.dumps(captured["parameters"], sort_keys=True).encode()).hexdigest()
    )


def test_bbr_is_scoped_to_the_ingest_bridge_namespace() -> None:
    resources = personal.runtime()["Resources"]
    for name, resource in resources.items():
        if resource["Type"] != "AWS::ECS::TaskDefinition":
            continue
        task = resource["Properties"]
        container = task["ContainerDefinitions"][0]
        if name == "IngestTask":
            assert task["NetworkMode"] == "bridge"
            assert container["SystemControls"] == [
                {"Namespace": "net.ipv4.tcp_congestion_control", "Value": "bbr"}
            ]
            assert container["Memory"] == 2048
            assert container["Cpu"] == 512
        else:
            assert "SystemControls" not in container


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
