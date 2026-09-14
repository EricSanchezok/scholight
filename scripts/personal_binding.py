"""Non-secret, immutable destination inputs for a reviewed production plan."""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

BUCKET = "scholight-personal-releases-669409472143-ap-south-2"
PROVIDERS = {"api": "SearchApi", "metadata": "SearchSync", "ingest": "SearchIngest"}


def digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def parameters(value: dict) -> dict:
    required = {
        "version",
        "endpoint",
        "papers_id",
        "chunks_id",
        "embedding_model",
        "embedding_dim",
        "secrets",
        "recovery_uri",
    }
    if set(value) != required or value["version"] != 1:
        raise ValueError("Unsupported destination binding")
    url = urlsplit(value["endpoint"])
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in ("", "/")
    ):
        raise ValueError("Destination endpoint must be HTTPS without embedded credentials")
    if value["embedding_dim"] != 1024 or value["embedding_model"] != "Qwen/Qwen3-Embedding-0.6B":
        raise ValueError("Destination must retain the reviewed embedding model and dimension")
    if any(not re.fullmatch(r"[0-9]+", value[name]) for name in ("papers_id", "chunks_id")):
        raise ValueError("Actual immutable collection IDs are required")
    if not re.fullmatch(rf"s3://{BUCKET}/recovery/des/[a-zA-Z0-9/_-]+", value["recovery_uri"]):
        raise ValueError("Recovery data must use the dedicated encrypted personal prefix")
    identity = {
        key: value[key]
        for key in ("endpoint", "papers_id", "chunks_id", "embedding_model", "embedding_dim")
    }
    result = {
        "RuntimeProfile": "full",
        "PublicThoroughEnabled": "true",
        "TargetEndpoint": value["endpoint"],
        "IngestionTargetId": digest(identity),
        "RecoveryUri": value["recovery_uri"],
        "EmbeddingModel": value["embedding_model"],
        "EmbeddingDimension": str(value["embedding_dim"]),
    }
    if set(value["secrets"]) != set(PROVIDERS):
        raise ValueError("Three independent application credential versions are required")
    seen = set()
    for component, prefix in PROVIDERS.items():
        secret = value["secrets"][component]
        if (
            set(secret) != {"arn", "version_id"}
            or not re.fullmatch(
                "arn:aws:secretsmanager:ap-south-2:669409472143:secret:"
                rf"/sanchezcloud/scholight/personal/des-{component}-[A-Za-z0-9]{{6}}",
                secret["arn"],
            )
            or not re.fullmatch(r"[A-Za-z0-9-]{32,64}", secret["version_id"])
        ):
            raise ValueError("Application credential must have its own personal ARN and version ID")
        if secret["arn"] in seen:
            raise ValueError("Application credentials must be independent")
        seen.add(secret["arn"])
        result[prefix + "SecretArn"] = secret["arn"]
        result[prefix + "SecretVersion"] = secret["version_id"]
    return result


def release_parameters(current: dict, manifest: dict, binding: dict | None = None) -> dict:
    result = dict(current)
    if binding is not None:
        incoming = parameters(binding)
        if (
            current.get("IngestionTargetId")
            and current["IngestionTargetId"] != incoming["IngestionTargetId"]
        ):
            raise ValueError("Changing an adopted target requires a separate reconciled cutover")
        result.update(incoming)
        if not current.get("IngestionTargetId"):
            result.update(MetadataEnabled="false", IngestEnabled="false")
    if manifest["version"] == 2:
        if not result.get("IngestionTargetId"):
            raise ValueError("A full release requires a verified destination binding")
    else:
        if binding is not None:
            raise ValueError("Legacy rollback cannot introduce a new destination binding")
        # N-1 code cannot write a target-bound cursor or prove fulltext versions.
        result.update(
            RuntimeProfile="lean",
            PublicThoroughEnabled="false",
            MetadataEnabled="false",
            IngestEnabled="false",
        )
    result.update({name.title() + "Image": image for name, image in manifest["images"].items()})
    return result


def read_binding(s3, key: str) -> tuple[dict, str]:
    if not re.fullmatch(r"bindings/des/[a-zA-Z0-9_-]+\.json", key):
        raise ValueError("An immutable destination binding key is required")
    body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    value = json.loads(body)
    parameters(value)
    return value, hashlib.sha256(body).hexdigest()


def verify_versions(secrets, values: dict) -> None:
    for prefix in PROVIDERS.values():
        description = secrets.describe_secret(SecretId=values[prefix + "SecretArn"])
        if description.get("DeletedDate") or values[
            prefix + "SecretVersion"
        ] not in description.get("VersionIdsToStages", {}):
            raise ValueError("A planned application credential version is no longer available")


def read_adoption(s3, key: str, target_id: str) -> str:
    if not re.fullmatch(r"recovery/des/[a-zA-Z0-9/_-]+/adoption\.json", key):
        raise ValueError("Enabling ingestion requires a reviewed destination adoption key")
    body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    value = json.loads(body)
    if (
        value.get("format") != "scholight.destination-adoption.v1"
        or value.get("target_id") != target_id
        or value.get("complete") is not True
    ):
        raise ValueError("Destination baseline and recovery scope have not been adopted")
    return hashlib.sha256(body).hexdigest()
