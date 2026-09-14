"""Immutable image and source contracts for personal production releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re

# Fixed Git executable with argument arrays; no shell is used.
import subprocess  # nosec B404

LEGACY_COMPONENTS = ("api", "web", "extract", "metadata")
COMPONENTS = (*LEGACY_COMPONENTS, "ingest")
ACCOUNT = "669409472143"
REGION = "ap-south-2"
IMAGE_CONTRACT = {
    "version": 2,
    "target_bound_ingestion": True,
    "public_search_modes": ["standard", "thorough"],
    "components": list(COMPONENTS),
}


def git(*args: str) -> bytes:
    # Only the controller supplies Git operations and validated revisions.
    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)  # nosec


def require_merged(sha: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("An exact merged commit is required")
    try:
        git("merge-base", "--is-ancestor", sha, "origin/main")
    except subprocess.CalledProcessError as exc:
        raise ValueError("Release source must be merged into main") from exc


def source_contract(sha: str) -> dict:
    require_merged(sha)
    names = git("ls-tree", "-r", "--name-only", sha, "migrations").decode().splitlines()
    migrations = {
        name: hashlib.sha256(git("show", f"{sha}:{name}")).hexdigest()
        for name in names
        if name.endswith(".sql")
    }
    lock = git("show", f"{sha}:uv.lock").decode()
    identity = re.search(r"sanchezcloud-identity\.git\?[^\"\s]+#([0-9a-f]{40})", lock)
    if not migrations or identity is None:
        raise ValueError("Missing immutable product migration or Identity contract")
    return {"identity_revision": identity.group(1), "migrations": migrations}


def image_contract(sha: str) -> dict:
    try:
        value = json.loads(git("show", f"{sha}:deploy/personal/image-contract.json"))
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise ValueError(
            "Source predates destination-aware publication; use its retained rollback manifest"
        ) from exc
    if value != IMAGE_CONTRACT:
        raise ValueError("Source image contract is unsupported by this controller")
    return value


def create(source_sha: str, control_revision: str, images: dict) -> dict:
    require_merged(source_sha)
    require_merged(control_revision)
    result = {
        "version": 2,
        "source_sha": source_sha,
        "control_revision": control_revision,
        "platform": "linux/arm64",
        "images": images,
        "image_contract": image_contract(source_sha),
        **source_contract(source_sha),
    }
    verify(result)
    return result


def verify(value: dict) -> None:
    if value.get("version") not in (1, 2) or value.get("platform") != "linux/arm64":
        raise ValueError("Unsupported production manifest version or architecture")
    require_merged(value["source_sha"])
    require_merged(value["control_revision"])
    components = LEGACY_COMPONENTS if value["version"] == 1 else COMPONENTS
    if set(value.get("images", {})) != set(components):
        raise ValueError("Every image component required by the manifest version must be present")
    for name, image in value["images"].items():
        if not re.fullmatch(
            rf"{ACCOUNT}\.dkr\.ecr\.{REGION}\.amazonaws\.com/scholight-personal-{name}@sha256:[0-9a-f]{{64}}",
            image,
        ):
            raise ValueError("Foreign or mutable release image")
    for name, expected in source_contract(value["source_sha"]).items():
        if value.get(name) != expected:
            raise ValueError("Release manifest does not match committed source")
    if value["version"] == 2 and value.get("image_contract") != image_contract(value["source_sha"]):
        raise ValueError("Application images lack the destination-aware runtime contract")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["create", "verify"])
    parser.add_argument("--file", required=True)
    args = parser.parse_args()
    if args.operation == "verify":
        with open(args.file) as source:
            verify(json.load(source))
        return
    registry = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"
    images = {
        name: f"{registry}/scholight-personal-{name}@" + os.environ[name.upper() + "_DIGEST"]
        for name in COMPONENTS
    }
    value = create(os.environ["SHA"], os.environ["GITHUB_SHA"], images)
    with open(args.file, "w") as output:
        json.dump(value, output, indent=2, sort_keys=True)


if __name__ == "__main__":
    main()
