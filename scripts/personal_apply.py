"""Guarded manual personal runtime changes and private ECS product migrations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess  # nosec B404
import tempfile
import time
from pathlib import Path

from personal_binding import read_adoption, read_binding, release_parameters, verify_versions
from personal_compatibility import expansion_required, receipt_key, verify_receipt
from personal_manifest import git, source_contract, verify
from personal_runtime import runtime
from release_admission import ACCOUNT, BUCKET, REGION, AdmissionRelease

STACK = "sanchezcloud-scholight-personal-runtime"


def aws(service: str, operation: str, *args: str) -> dict:
    # Fixed executable and argument array; operations originate in this controller.
    command = [
        "aws",
        service,
        operation,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
        "--no-cli-pager",
        *args,
    ]
    options = {"capture_output": True, "text": True, "check": False, "timeout": 60}
    result = subprocess.run(command, **options)  # nosec
    if result.returncode:
        raise RuntimeError(f"AWS {service} {operation} failed")
    return json.loads(result.stdout or "{}")


def validate_changes(change: dict) -> None:
    if change["StackName"] != STACK or change["Status"] != "CREATE_COMPLETE":
        raise ValueError("Change set is not an available personal runtime plan")
    for entry in change["Changes"]:
        r = entry["ResourceChange"]
        if r["ResourceType"] not in {
            "AWS::ECS::TaskDefinition",
            "AWS::ECS::Service",
            "AWS::IAM::Role",
            "AWS::IAM::Policy",
            "AWS::SSM::Parameter",
            "AWS::Logs::LogGroup",
        }:
            raise ValueError("Unexpected resource outside the product runtime")
        if r["Action"] == "Remove":
            raise ValueError("Runtime removal requires a separate reviewed recovery procedure")
        if (
            r.get("Replacement") in ("True", "Conditional")
            and r["ResourceType"] != "AWS::ECS::TaskDefinition"
        ):
            raise ValueError("Only immutable task definitions may be replaced")


def guard_plan(plan: dict, control: str, digest: str, updated: float, now: float) -> None:
    if plan["control"] != control or plan["manifest_sha256"] != digest:
        raise ValueError("Controller or manifest changed since review")
    if not 0 <= now - plan["created_at"] <= 86400:
        raise ValueError("Plan expired; prepare and review a new plan")
    if updated != plan["stack_updated_at"]:
        raise ValueError("Runtime changed after this plan was created")


def read_manifest(s3, key: str) -> tuple[dict, str]:
    if not re.fullmatch(r"releases/[0-9a-f]{40}/arm64-[0-9]+-[0-9]+\.json", key):
        raise ValueError("An immutable personal release manifest key is required")
    body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    value = json.loads(body)
    verify(value)
    if key.split("/")[1] != value["source_sha"]:
        raise ValueError("Manifest key does not match its merged source")
    return value, hashlib.sha256(body).hexdigest()


def compatible_with_running(parameters: dict, manifest: dict) -> bool:
    image = parameters["ApiImage"]
    digest = image.split("@", 1)[1]
    details = aws(
        "ecr",
        "describe-images",
        "--repository-name",
        "scholight-personal-api",
        "--image-ids",
        "imageDigest=" + digest,
    )["imageDetails"]
    revisions = {
        tag[4:-6]
        for d in details
        for tag in d.get("imageTags", [])
        if re.fullmatch(r"git-[0-9a-f]{40}-arm64", tag)
    }
    if len(revisions) != 1:
        raise ValueError("Cannot prove the currently deployed migration contract")
    return expansion_required(source_contract(revisions.pop()), manifest)


def require_migration_receipt(s3, parameters: dict, manifest: dict) -> None:
    from botocore.exceptions import ClientError

    try:
        body = s3.get_object(Bucket=BUCKET, Key=receipt_key(manifest))["Body"].read()
    except ClientError as exc:
        raise ValueError(
            "Run the separately reviewed compatibility migration before apply"
        ) from exc
    verify_receipt(json.loads(body), parameters, manifest)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["plan", "apply", "migrate"])
    parser.add_argument("--manifest-key")
    parser.add_argument("--change-set")
    parser.add_argument("--binding-key")
    parser.add_argument("--resume-ingestion", action="store_true")
    parser.add_argument("--adoption-key")
    args = parser.parse_args()
    account, region = os.environ["EXPECTED_ACCOUNT_ID"], os.environ["AWS_REGION"]
    if account != ACCOUNT or region != REGION:
        raise ValueError("Wrong personal account or region")
    if aws("sts", "get-caller-identity")["Account"] != account:
        raise ValueError("Unexpected credential identity")
    control = git("rev-parse", "HEAD").decode().strip()
    if control != git("rev-parse", "origin/main").decode().strip():
        raise ValueError("Use the current reviewed main controller")
    import boto3

    s3 = boto3.client("s3", region_name=REGION)
    manifest, digest = read_manifest(s3, args.manifest_key or "")
    if args.operation == "migrate":
        from personal_migration import execute

        execute(manifest, digest)
        return
    binding, binding_digest = (
        read_binding(s3, args.binding_key) if args.binding_key else (None, None)
    )
    if args.operation == "plan":
        stack = aws("cloudformation", "describe-stacks", "--stack-name", STACK)["Stacks"][0]
        if stack["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
            raise ValueError("Runtime must be stable before planning")
        parameters = {p["ParameterKey"]: p["ParameterValue"] for p in stack["Parameters"]}
        migration_required = compatible_with_running(parameters, manifest)
        parameters = release_parameters(parameters, manifest, binding)
        template = runtime()
        for name, specification in template["Parameters"].items():
            if name not in parameters and "Default" in specification:
                parameters[name] = str(specification["Default"])
        adoption_digest = None
        if args.resume_ingestion:
            if manifest["version"] != 2:
                raise ValueError("Legacy consumers cannot resume ingestion")
            parameters.update(MetadataEnabled="true", IngestEnabled="true")
            adoption_digest = read_adoption(
                s3, args.adoption_key or "", parameters["IngestionTargetId"]
            )
        verify_versions(boto3.client("secretsmanager", region_name=REGION), parameters)
        with tempfile.TemporaryDirectory() as directory:
            body = Path(directory) / "runtime.json"
            body.write_text(json.dumps(runtime()))
            values = Path(directory) / "parameters.json"
            values.write_text(
                json.dumps(
                    [{"ParameterKey": k, "ParameterValue": v} for k, v in parameters.items()]
                )
            )
            result = aws(
                "cloudformation",
                "create-change-set",
                "--stack-name",
                STACK,
                "--change-set-name",
                "manual-" + str(int(time.time())),
                "--change-set-type",
                "UPDATE",
                "--template-body",
                "file://" + str(body),
                "--parameters",
                "file://" + str(values),
                "--capabilities",
                "CAPABILITY_NAMED_IAM",
                "--role-arn",
                os.environ["CLOUDFORMATION_ROLE_ARN"],
                "--description",
                f"personal:runtime:{control}:{digest}",
            )
        release = AdmissionRelease(result["Id"])
        from datetime import datetime

        updated = datetime.fromisoformat(
            stack.get("LastUpdatedTime", stack["CreationTime"])
        ).timestamp()
        release.record(
            "plan",
            {
                "control": control,
                "manifest_key": args.manifest_key,
                "manifest_sha256": digest,
                "created_at": time.time(),
                "stack_updated_at": updated,
                "binding_key": args.binding_key,
                "binding_sha256": binding_digest,
                "migration_required": migration_required,
                "adoption_key": args.adoption_key if args.resume_ingestion else None,
                "adoption_sha256": adoption_digest,
                "parameters_sha256": hashlib.sha256(
                    json.dumps(parameters, sort_keys=True).encode()
                ).hexdigest(),
            },
        )
        for _ in range(60):
            change = release.cf.describe_change_set(ChangeSetName=result["Id"])
            if change["Status"] not in {"CREATE_PENDING", "CREATE_IN_PROGRESS"}:
                break
            time.sleep(5)
        validate_changes(change)
        print(
            json.dumps(
                {
                    "account": account,
                    "region": region,
                    "source_sha": manifest["source_sha"],
                    "change_set": result["Id"],
                    "changes": change["Changes"],
                    "migration_required_before_apply": migration_required,
                    "target": {
                        name: parameters[name]
                        for name in (
                            "TargetEndpoint",
                            "IngestionTargetId",
                            "RuntimeProfile",
                            "MetadataEnabled",
                            "IngestEnabled",
                        )
                    },
                    "credential_versions": {
                        name: value
                        for name, value in parameters.items()
                        if name.endswith("SecretVersion")
                    },
                },
                default=str,
            )
        )
    else:
        if not args.change_set or not re.fullmatch(
            rf"arn:aws:cloudformation:{region}:{account}:changeSet/[\w-]+/[\w-]+", args.change_set
        ):
            raise ValueError("An exact change set ARN in the personal account is required")
        release = AdmissionRelease(args.change_set)
        plan = release.read("plan")
        if plan is None or plan["manifest_key"] != args.manifest_key:
            raise ValueError("No matching durable release plan")
        if (
            plan.get("binding_key") != args.binding_key
            or plan.get("binding_sha256") != binding_digest
        ):
            raise ValueError("Destination binding changed after plan")
        if release.read("start") is None:
            stack = release.stack(STACK)
            updated = stack.get("LastUpdatedTime", stack["CreationTime"]).timestamp()
            guard_plan(plan, control, digest, updated, time.time())
            change = release.cf.describe_change_set(ChangeSetName=args.change_set)
            validate_changes(change)
            if (
                change["Description"] != f"personal:runtime:{control}:{digest}"
                or change["ExecutionStatus"] != "AVAILABLE"
            ):
                raise ValueError("Plan no longer executable or bound to this manifest")
            parameters = {p["ParameterKey"]: p["ParameterValue"] for p in change["Parameters"]}
            if (
                hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()
                != plan["parameters_sha256"]
            ):
                raise ValueError("Planned runtime configuration changed")
            verify_versions(boto3.client("secretsmanager", region_name=REGION), parameters)
            if (
                plan.get("adoption_key")
                and read_adoption(s3, plan["adoption_key"], parameters["IngestionTargetId"])
                != plan["adoption_sha256"]
            ):
                raise ValueError("Baseline adoption proof changed after review")
            if plan.get("migration_required"):
                require_migration_receipt(s3, parameters, manifest)
            if any(
                parameters[name.title() + "Image"] != value
                for name, value in manifest["images"].items()
            ):
                raise ValueError("Planned images differ from the manifest")
        elif plan["control"] != control or plan["manifest_sha256"] != digest:
            raise ValueError("Resume requires the original reviewed controller and manifest")
        release.run()


if __name__ == "__main__":
    main()
