import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.release_manifest import IMAGE_COMPONENTS, create_manifest, verify_manifest

SHA = "a" * 40


def _image(component: str, digest: str = "b" * 64) -> str:
    return (
        "919651863140.dkr.ecr.ap-southeast-1.amazonaws.com/"
        f"sanchezcloud-scholight-{component}@sha256:{digest}"
    )


def _manifest() -> dict[str, object]:
    return create_manifest(
        argparse.Namespace(
            release_sha=SHA,
            identity_schema_version=3,
            **{f"{component}_image": _image(component) for component in IMAGE_COMPONENTS},
        )
    )


def test_manifest_records_all_immutable_release_inputs() -> None:
    manifest = _manifest()

    assert manifest["contract_version"] == 1
    assert manifest["release_sha"] == SHA
    assert set(manifest["images"]) == set(IMAGE_COMPONENTS)  # type: ignore[arg-type]
    assert manifest["identity"]["schema_version"] == 3  # type: ignore[index]
    assert manifest["scholight_migrations"]["latest"].endswith(".sql")  # type: ignore[index,union-attr]
    assert len(manifest["runtime_template_sha256"]) == 64  # type: ignore[arg-type]


def test_manifest_is_deterministic_and_verifies_against_source() -> None:
    first = _manifest()
    second = json.loads(json.dumps(_manifest(), sort_keys=True))

    assert first == second
    verify_manifest(first, SHA)


def test_manifest_rejects_wrong_component_repository() -> None:
    with pytest.raises(ValueError, match="web image"):
        create_manifest(
            argparse.Namespace(
                release_sha=SHA,
                identity_schema_version=3,
                **{
                    f"{component}_image": _image("api") if component == "web" else _image(component)
                    for component in IMAGE_COMPONENTS
                },
            )
        )


def test_manifest_rejects_tampering_and_wrong_requested_sha() -> None:
    manifest = _manifest()
    manifest["runtime_template_sha256"] = "c" * 64

    with pytest.raises(ValueError, match="does not match"):
        verify_manifest(manifest, SHA)
    with pytest.raises(ValueError, match="requested release"):
        verify_manifest(_manifest(), "d" * 40)


def test_cli_writes_and_verifies_manifest(tmp_path: Path) -> None:
    output = tmp_path / "manifest.json"
    command = [
        sys.executable,
        "scripts/release_manifest.py",
        "create",
        "--release-sha",
        SHA,
        "--identity-schema-version",
        "3",
    ]
    for component in IMAGE_COMPONENTS:
        command.extend((f"--{component}-image", _image(component)))
    command.extend(("--output", str(output)))

    subprocess.run(command, check=True)
    subprocess.run(
        [
            sys.executable,
            "scripts/release_manifest.py",
            "verify",
            "--manifest",
            str(output),
            "--expected-release-sha",
            SHA,
        ],
        check=True,
    )
