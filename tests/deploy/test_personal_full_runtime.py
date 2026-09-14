"""Fulltext shares admission, never an unbounded or always-on worker service."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def runtime():
    spec = importlib.util.spec_from_file_location("runtime", ROOT / "scripts/personal_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.runtime()


def test_ingest_has_bounded_resources_and_no_service():
    resources = runtime()["Resources"]
    task = resources["IngestTask"]["Properties"]
    assert task["Memory"] == "2048"
    assert task["Cpu"] == "512"
    assert "IngestService" not in resources
    container = task["ContainerDefinitions"][0]
    assert container["Command"] == [
        "scholight",
        "scheduler",
        "drain-ingest",
        "--max-runtime-seconds",
        "1800",
    ]
    env = {v["Name"]: v["Value"] for v in container["Environment"]}
    assert env["SCHOLIGHT_PG_POOL_MAX_SIZE"] == "2"
    assert env["SCHOLIGHT_EMBEDDING_BATCH_SIZE"] == "64"
    assert env["SCHOLIGHT_SURVEY_RUNTIME_ENABLED"] == "false"
    assert env["SCHOLIGHT_INGESTION_TARGET_ID"] == {"Ref": "IngestionTargetId"}


def test_ingest_registration_is_disabled_until_verified_cutover():
    template = runtime()
    assert template["Parameters"]["IngestEnabled"]["Default"] == "false"
    value = template["Resources"]["IngestAdmissionRegistration"]["Properties"]["Value"]["Fn::Sub"]
    assert '"interval_seconds":3600' in value
    assert '"memory_mib":2048' in value
    assert "${IngestEnabled}" in value


def test_search_and_workers_use_independent_versioned_secrets():
    resources = runtime()["Resources"]
    for name, provider in (
        ("Api", "SearchApi"),
        ("Metadata", "SearchSync"),
        ("Ingest", "SearchIngest"),
    ):
        container = resources[name + "Task"]["Properties"]["ContainerDefinitions"][0]
        values = {value["Name"]: value["ValueFrom"] for value in container["Secrets"]}
        assert values["SCHOLIGHT_ZILLIZ_TOKEN"] == {
            "Fn::Sub": "${" + provider + "SecretArn}:zilliz_token::${" + provider + "SecretVersion}"
        }
        assert "SCHOLIGHT_ZILLIZ_URI" not in values
        env = {v["Name"]: v["Value"] for v in container["Environment"]}
        assert env["SCHOLIGHT_ZILLIZ_URI"] == {"Ref": "TargetEndpoint"}


def test_ingest_permissions_only_cover_recovery_prefix_and_exact_task_revision():
    resources = runtime()["Resources"]
    statements = resources["IngestRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    writes = [s for s in statements if "s3:PutObject" in s["Action"]]
    assert writes[0]["Resource"]["Fn::Sub"].endswith("/recovery/des/*")
    grants = resources["IngestAdmissionGrant"]["Properties"]["PolicyDocument"]["Statement"]
    assert grants[0]["Resource"] == {"Ref": "IngestTask"}
