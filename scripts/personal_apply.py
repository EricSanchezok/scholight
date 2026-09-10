"""Guarded manual personal runtime changes and private ECS product migrations."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from personal_runtime import runtime

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
        if r["Action"] == "Remove":
            raise ValueError("Runtime removal requires a separate reviewed recovery procedure")
        if (
            r.get("Replacement") in ("True", "Conditional")
            and r["ResourceType"] != "AWS::ECS::TaskDefinition"
        ):
            raise ValueError("Only immutable task definitions may be replaced")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["plan", "apply", "migrate"])
    parser.add_argument("--parameters", type=Path)
    parser.add_argument("--change-set")
    args = parser.parse_args()
    account = os.environ["EXPECTED_ACCOUNT_ID"]
    region = os.environ["AWS_REGION"]
    if account != "669409472143" or region != "ap-south-2":
        raise ValueError("Wrong personal account or region")
    if aws("sts", "get-caller-identity")["Account"] != account:
        raise ValueError("Unexpected credential identity")
    if args.operation == "plan":
        if not args.parameters:
            raise ValueError("An explicit parameter file is required")
        parameters = json.loads(args.parameters.read_text())
        template = runtime()
        if (
            set(parameters) - set(template["Parameters"])
            or parameters.get("ExpectedAccountId") != account
        ):
            raise ValueError("Unknown parameters or wrong target account")
        # No credentials are accepted: only known CFN references, public CA and configuration.
        with tempfile.TemporaryDirectory() as directory:
            body = Path(directory) / "runtime.json"
            body.write_text(json.dumps(template))
            values = Path(directory) / "parameters.json"
            values.write_text(
                json.dumps(
                    [{"ParameterKey": k, "ParameterValue": str(v)} for k, v in parameters.items()]
                )
            )
            try:
                aws("cloudformation", "describe-stacks", "--stack-name", STACK)
                kind = "UPDATE"
            except RuntimeError:
                kind = "CREATE"
            result = aws(
                "cloudformation",
                "create-change-set",
                "--stack-name",
                STACK,
                "--change-set-name",
                "manual-" + str(int(time.time())),
                "--change-set-type",
                kind,
                "--template-body",
                "file://" + str(body),
                "--parameters",
                "file://" + str(values),
                "--capabilities",
                "CAPABILITY_NAMED_IAM",
                "--role-arn",
                os.environ["CLOUDFORMATION_ROLE_ARN"],
            )
        print(json.dumps({"change_set": result["Id"]}))
    elif args.operation == "apply":
        if not args.change_set or not re.fullmatch(
            rf"arn:aws:cloudformation:{region}:{account}:changeSet/[\w-]+/[\w-]+", args.change_set
        ):
            raise ValueError("An exact change set ARN in the personal account is required")
        change = aws(
            "cloudformation",
            "describe-change-set",
            "--stack-name",
            STACK,
            "--change-set-name",
            args.change_set,
        )
        validate_changes(change)
        print(
            json.dumps(
                {
                    "changes": [
                        {
                            k: r["ResourceChange"].get(k)
                            for k in ("Action", "LogicalResourceId", "ResourceType", "Replacement")
                        }
                        for r in change["Changes"]
                    ]
                }
            )
        )
        aws(
            "cloudformation",
            "execute-change-set",
            "--stack-name",
            STACK,
            "--change-set-name",
            args.change_set,
        )
    else:
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
