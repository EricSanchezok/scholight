"""Guarded manual personal runtime changes and private ECS product migrations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from personal_manifest import git, source_contract, verify
from personal_runtime import runtime
from release_admission import ACCOUNT, BUCKET, REGION, AdmissionRelease

STACK = "sanchezcloud-scholight-personal-runtime"


def aws(service: str, operation: str, *args: str) -> dict:
    result = subprocess.run(
        [
            "aws",
            service,
            operation,
            "--region",
            os.environ["AWS_REGION"],
            "--output",
            "json",
            "--no-cli-pager",
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
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


def compatible_with_running(parameters: dict, manifest: dict) -> None:
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
    for name, value in source_contract(revisions.pop()).items():
        if manifest[name] != value:
            raise ValueError(
                "Migration or Identity contract changed; a separately reviewed compatibility migration is required"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["plan", "apply", "migrate"])
    parser.add_argument("--manifest-key")
    parser.add_argument("--change-set")
    args = parser.parse_args()
    account, region = os.environ["EXPECTED_ACCOUNT_ID"], os.environ["AWS_REGION"]
    if account != ACCOUNT or region != REGION:
        raise ValueError("Wrong personal account or region")
    if aws("sts", "get-caller-identity")["Account"] != account:
        raise ValueError("Unexpected credential identity")
    control = git("rev-parse", "HEAD").decode().strip()
    if control != git("rev-parse", "origin/main").decode().strip():
        raise ValueError("Use the current reviewed main controller")
    if args.operation == "migrate":
        migrate(account, region)
        return
    import boto3

    s3 = boto3.client("s3", region_name=REGION)
    manifest, digest = read_manifest(s3, args.manifest_key or "")
    if args.operation == "plan":
        stack = aws("cloudformation", "describe-stacks", "--stack-name", STACK)["Stacks"][0]
        if stack["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
            raise ValueError("Runtime must be stable before planning")
        parameters = {p["ParameterKey"]: p["ParameterValue"] for p in stack["Parameters"]}
        compatible_with_running(parameters, manifest)
        parameters.update(
            {name.title() + "Image": image for name, image in manifest["images"].items()}
        )
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
            if any(
                parameters[name.title() + "Image"] != value
                for name, value in manifest["images"].items()
            ):
                raise ValueError("Planned images differ from the manifest")
        elif plan["control"] != control or plan["manifest_sha256"] != digest:
            raise ValueError("Resume requires the original reviewed controller and manifest")
        release.run()


def migrate(account: str, region: str) -> None:
    stack = aws("cloudformation", "describe-stacks", "--stack-name", STACK)["Stacks"][0]
    outputs = {v["OutputKey"]: v["OutputValue"] for v in stack["Outputs"]}
    task = outputs["MigrationTaskDefinitionArn"]
    if not task.startswith(
        f"arn:aws:ecs:{region}:{account}:task-definition/scholight-personal-migration:"
    ):
        raise ValueError("Unexpected migration task")
    cluster = os.environ["ECS_CLUSTER_ARN"]
    response = aws(
        "ecs",
        "run-task",
        "--cluster",
        cluster,
        "--launch-type",
        "EC2",
        "--task-definition",
        task,
        "--count",
        "1",
        "--started-by",
        "github-product-migration",
    )
    if response.get("failures") or len(response.get("tasks", [])) != 1:
        raise RuntimeError("Migration did not start")
    task_arn = response["tasks"][0]["taskArn"]
    print(json.dumps({"migration_task": task_arn}), flush=True)
    for _ in range(120):
        description = aws("ecs", "describe-tasks", "--cluster", cluster, "--tasks", task_arn)[
            "tasks"
        ][0]
        if description["lastStatus"] == "STOPPED":
            if not description.get("containers") or any(
                c.get("exitCode") != 0 for c in description["containers"]
            ):
                raise RuntimeError("Product migration failed; inspect its protected log group")
            print("Product migration completed")
            return
        time.sleep(10)
    aws(
        "ecs",
        "stop-task",
        "--cluster",
        cluster,
        "--task",
        task_arn,
        "--reason",
        "Manual migration exceeded twenty minute limit",
    )
    raise RuntimeError("Product migration timed out")


if __name__ == "__main__":
    main()
