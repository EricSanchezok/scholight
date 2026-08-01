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
