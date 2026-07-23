"""Keep every production cloud-auth consumer on the locked dependency revision."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _locked_revision() -> str:
    with (ROOT / "pyproject.toml").open("rb") as file:
        project = tomllib.load(file)
    return str(project["tool"]["uv"]["sources"]["cloud-auth"]["rev"])


def test_backend_image_uses_locked_cloud_auth_revision() -> None:
    dockerfile = (ROOT / "docker/scholight-api/Dockerfile").read_text()

    assert f"ARG PRIVATE_DEP_REVISION={_locked_revision()}" in dockerfile


def test_migration_contract_uses_locked_cloud_auth_revision() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()

    assert f"ref: {_locked_revision()}" in workflow
