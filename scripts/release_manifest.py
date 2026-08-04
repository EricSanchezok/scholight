#!/usr/bin/env python3
"""Create and verify immutable Scholight ECS release manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
IMAGE_COMPONENTS = ("web", "api", "extract", "ingest", "survey")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
IMAGE_PATTERN = re.compile(
    r"[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/"
    r"sanchezcloud-scholight-(web|api|extract|ingest|survey)@sha256:[0-9a-f]{64}"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _migration_contract() -> dict[str, str]:
    migrations = sorted((ROOT / "migrations").glob("*.sql"))
    if not migrations:
        raise ValueError("no Scholight migrations found")
    digest = hashlib.sha256()
    for migration in migrations:
        digest.update(migration.name.encode())
        digest.update(b"\0")
        digest.update(migration.read_bytes())
        digest.update(b"\0")
    return {"latest": migrations[-1].name, "checksum": digest.hexdigest()}


def _identity_contract(schema_version: int) -> dict[str, Any]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    tag = pyproject["tool"]["uv"]["sources"]["sanchezcloud-identity"]["tag"]
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    match = re.search(
        r"sanchezcloud-identity\.git\?(?:rev|tag)=[^#\"\s]+#([0-9a-f]{40})",
        lock,
    )
    if match is None:
        raise ValueError("uv.lock does not pin SanchezCloud Identity to a commit SHA")
    return {"tag": tag, "commit_sha": match.group(1), "schema_version": schema_version}


def _rcm_contract() -> dict[str, str]:
    dockerfile = (ROOT / "docker/scholight-api/Dockerfile").read_text(encoding="utf-8")
    version = re.search(r"^ARG RCM_VERSION=(\S+)$", dockerfile, re.MULTILINE)
    checksum = re.search(r"^ARG RCM_LINUX_X86_64_SHA256=([0-9a-f]{64})$", dockerfile, re.MULTILINE)
    if version is None or checksum is None:
        raise ValueError("Survey image does not pin an RCM version and checksum")
    return {"version": version.group(1), "sha256": checksum.group(1)}


def create_manifest(args: argparse.Namespace) -> dict[str, Any]:
    if SHA_PATTERN.fullmatch(args.release_sha) is None:
        raise ValueError("release SHA must be a lowercase 40-character commit SHA")
    if not isinstance(args.identity_schema_version, int) or args.identity_schema_version < 1:
        raise ValueError("Identity schema version must be a positive integer")
    images = {component: getattr(args, f"{component}_image") for component in IMAGE_COMPONENTS}
    for component, image in images.items():
        match = IMAGE_PATTERN.fullmatch(image)
        if match is None or match.group(1) != component:
            raise ValueError(f"invalid digest-qualified {component} image")
    return {
        "contract_version": 1,
        "release_sha": args.release_sha,
        "images": images,
        "identity": _identity_contract(args.identity_schema_version),
        "scholight_migrations": _migration_contract(),
        "rcm": _rcm_contract(),
        "runtime_template_sha256": _sha256(ROOT / "deploy/ecs/scholight-production.yml"),
    }


def verify_manifest(manifest: dict[str, Any], expected_release_sha: str | None = None) -> None:
    if manifest.get("contract_version") != 1:
        raise ValueError("unsupported release manifest contract")
    release_sha = manifest.get("release_sha")
    if not isinstance(release_sha, str) or SHA_PATTERN.fullmatch(release_sha) is None:
        raise ValueError("invalid release SHA")
    if expected_release_sha is not None and release_sha != expected_release_sha:
        raise ValueError("release manifest SHA does not match the requested release")
    expected = create_manifest(
        argparse.Namespace(
            release_sha=release_sha,
            identity_schema_version=manifest.get("identity", {}).get("schema_version"),
            **{
                f"{name}_image": manifest.get("images", {}).get(name, "")
                for name in IMAGE_COMPONENTS
            },
        )
    )
    if manifest != expected:
        raise ValueError("release manifest does not match the checked-out source contract")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--release-sha", required=True)
    create.add_argument("--identity-schema-version", required=True, type=int)
    for component in IMAGE_COMPONENTS:
        create.add_argument(f"--{component}-image", required=True)
    create.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--expected-release-sha")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "create":
            manifest = create_manifest(args)
            args.output.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        else:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            verify_manifest(manifest, args.expected_release_sha)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"release manifest error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
