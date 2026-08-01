"""Keep every production Identity SDK consumer on the locked revision."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _locked_revision() -> str:
    with (ROOT / "pyproject.toml").open("rb") as file:
        project = tomllib.load(file)
    return str(project["tool"]["uv"]["sources"]["sanchezcloud-identity"]["rev"])


def test_backend_image_uses_locked_identity_revision() -> None:
    dockerfile = (ROOT / "docker/scholight-api/Dockerfile").read_text()

    assert f"ARG SANCHEZCLOUD_IDENTITY_REVISION={_locked_revision()}" in dockerfile


def test_migration_contract_uses_locked_identity_revision() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()

    assert f"ref: {_locked_revision()}" in workflow


def test_workflows_use_the_scoped_dependency_reader_app() -> None:
    workflows = "\n".join(
        (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        for name in ("ci.yml", "release.yml")
    )

    assert "actions/create-github-app-token@" in workflows
    assert "vars.IDENTITY_READER_APP_ID" in workflows
    assert "secrets.IDENTITY_READER_PRIVATE_KEY" in workflows
    assert "permission-contents: read" in workflows
    assert "CLOUD_AUTH_READ_TOKEN" not in workflows


def test_candidate_identity_compatibility_workflow_is_standardized() -> None:
    workflow = (ROOT / ".github" / "workflows" / "sanchezcloud-identity-compat.yml").read_text(
        encoding="utf-8"
    )

    for input_name in (
        "identity_ref",
        "version",
        "schema_version",
        "correlation_id",
    ):
        assert f"{input_name}:" in workflow
    assert "actions/create-github-app-token@" in workflow
    assert "permission-contents: read" in workflow
    assert "secrets.IDENTITY_READER_PRIVATE_KEY" in workflow
    assert "uv pip install" in workflow
    assert "AUTH_SCHEMA_VERSION" in workflow
    assert "sanchezcloud-identity migrate" in workflow
    assert "SANCHEZCLOUD_IDENTITY_REVISION" in workflow
    assert "CLOUD_AUTH_READ_TOKEN" not in workflow
