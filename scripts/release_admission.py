"""Drain and atomically refresh this product's admitted worker registrations."""

from __future__ import annotations

import json
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ACCOUNT = "669409472143"
REGION = "ap-south-2"
CLUSTER = "sanchezcloud-personal"
RUNTIME = "sanchezcloud-scholight-personal-runtime"
BACKGROUND = RUNTIME
BUCKET = f"scholight-personal-releases-{ACCOUNT}-{REGION}"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/ScholightPersonalCloudFormation"
NAMES = {"scholight-metadata"}
PREFIX = "/sanchezcloud/personal/background/"


def acknowledged(status: dict, expected: dict, now: float) -> bool:
    return (
        status.get("account") == ACCOUNT
        and status.get("region") == REGION
        and status.get("cluster") == CLUSTER
        and 0 <= now - status.get("observed_at", 0) <= 90
        and status.get("registration_refreshed_at", 0) > 0
        and all(status.get("registrations", {}).get(k) == v for k, v in expected.items())
    )


def unsettled(tasks: list[dict], names: set[str]) -> bool:
    return any(
        t.get("group") in {"background:" + n for n in names} and t.get("lastStatus") != "STOPPED"
        for t in tasks
    )


def guard_registration_change(change: dict) -> None:
    allowed = {"AdmissionRegistration"}
    for item in change.get("Changes", []):
        r = item["ResourceChange"]
        if (
            r["Action"] != "Modify"
            or r["LogicalResourceId"] not in allowed
            or r.get("Replacement") in {"True", "Conditional"}
        ):
            raise ValueError("Only existing product registrations and their grant may change")


def guard_candidate_state(actual: dict, previous: dict, candidate: dict) -> None:
    if actual not in (previous | {"MetadataEnabled": "false"}, candidate):
        raise ValueError("Runtime changed outside this release; admission stays paused")


class AdmissionRelease:
    def __init__(self, change_arn: str):
        session = boto3.Session(region_name=REGION)
        cfg = Config(connect_timeout=8, read_timeout=30, retries={"total_max_attempts": 3})
        if session.client("sts", config=cfg).get_caller_identity()["Account"] != ACCOUNT:
            raise ValueError("Unexpected deployment account")
        self.cf = session.client("cloudformation", config=cfg)
        self.ecs = session.client("ecs", config=cfg)
        self.ssm = session.client("ssm", config=cfg)
        self.s3 = session.client("s3", config=cfg)
        self.arn = change_arn
        self.operation = change_arn.rsplit("/", 1)[-1]
        self.prefix = f"cloudformation/personal/releases/{self.operation}/"

    def read(self, name: str) -> dict | None:
        key = self.prefix + name + ".json"
        listing = self.s3.list_objects_v2(Bucket=BUCKET, Prefix=key, MaxKeys=1)
        if not any(value["Key"] == key for value in listing.get("Contents", [])):
            return None
        return json.loads(self.s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())

    def record(self, name: str, value: dict) -> None:
        previous = self.read(name)
        if previous is not None:
            if previous != value:
                raise ValueError("Release checkpoint conflicts with the original operation")
            return
        self.s3.put_object(
            Bucket=BUCKET,
            Key=self.prefix + name + ".json",
            Body=json.dumps(value, sort_keys=True).encode(),
            ContentType="application/json",
            IfNoneMatch="*",
        )

    def stack(self, name: str) -> dict:
        return self.cf.describe_stacks(StackName=name)["Stacks"][0]

    def wait_stack(self, name: str) -> None:
        for _ in range(180):
            status = self.stack(name)["StackStatus"]
            if status == "UPDATE_COMPLETE":
                return
            if not status.endswith("IN_PROGRESS"):
                raise RuntimeError(
                    f"Stack did not converge: {name} {status}; admission stays paused"
                )
            time.sleep(10)
        raise TimeoutError("Stack update needs investigation; admission stays paused")

    def change_background(self, stage: str, overrides: dict) -> None:
        name = "release-" + self.operation + "-" + stage
        current = self.stack(BACKGROUND)
        actual = {p["ParameterKey"]: p["ParameterValue"] for p in current["Parameters"]}
        if current["StackStatus"].endswith("IN_PROGRESS"):
            self.wait_stack(BACKGROUND)
            current = self.stack(BACKGROUND)
            actual = {p["ParameterKey"]: p["ParameterValue"] for p in current["Parameters"]}
        if all(actual.get(k) == v for k, v in overrides.items()):
            return
        if set(overrides) - set(actual):
            raise ValueError("Unknown registration parameter")
        try:
            change = self.cf.describe_change_set(StackName=BACKGROUND, ChangeSetName=name)
        except ClientError as exc:
            if "does not exist" not in str(exc):
                raise
            result = self.cf.create_change_set(
                StackName=BACKGROUND,
                ChangeSetName=name,
                ChangeSetType="UPDATE",
                UsePreviousTemplate=True,
                Description=f"product-release:{self.operation}:{stage}",
                Parameters=[
                    {"ParameterKey": k, "ParameterValue": overrides.get(k, v)}
                    for k, v in actual.items()
                ],
                Capabilities=["CAPABILITY_NAMED_IAM"],
                RoleARN=ROLE,
            )
            for _ in range(60):
                change = self.cf.describe_change_set(ChangeSetName=result["Id"])
                if change["Status"] not in {"CREATE_PENDING", "CREATE_IN_PROGRESS"}:
                    break
                time.sleep(5)
        if change["Status"] != "CREATE_COMPLETE" or change["ExecutionStatus"] != "AVAILABLE":
            raise ValueError("Registration change set is not executable")
        guard_registration_change(change)
        planned = {p["ParameterKey"]: p["ParameterValue"] for p in change["Parameters"]}
        if planned != actual | overrides:
            raise ValueError("Registration plan no longer matches live configuration")
        print(
            json.dumps(
                {
                    "registration_change_set": change["ChangeSetId"],
                    "stage": stage,
                    "changes": [
                        x["ResourceChange"]["LogicalResourceId"] for x in change["Changes"]
                    ],
                }
            ),
            flush=True,
        )
        self.cf.execute_change_set(ChangeSetName=change["ChangeSetId"])
        self.wait_stack(BACKGROUND)

    def wait_ack(self) -> None:
        parameters = self.ssm.get_parameters(Names=[PREFIX + n for n in sorted(NAMES)])
        if parameters.get("InvalidParameters") or len(parameters["Parameters"]) != len(NAMES):
            raise ValueError("Product registration missing")
        expected = {}
        for p in parameters["Parameters"]:
            value = json.loads(p["Value"])
            expected[value["name"]] = {
                "parameter_version": p["Version"],
                "enabled": value["enabled"],
                "task_definition": value["task_definition"],
            }
        for _ in range(60):
            raw = self.ssm.get_parameter(Name="/sanchezcloud/personal/admission-status")[
                "Parameter"
            ]["Value"]
            if acknowledged(json.loads(raw), expected, time.time()):
                return
            time.sleep(10)
        raise TimeoutError("Controller did not acknowledge registration versions; do not proceed")

    def wait_drained(self) -> None:
        for _ in range(120):
            arns = set()
            # STOPPING tasks can already have desiredStatus=STOPPED. Both lists are required.
            for status in ("RUNNING", "STOPPED"):
                for p in self.ecs.get_paginator("list_tasks").paginate(
                    cluster=CLUSTER, desiredStatus=status
                ):
                    arns.update(p["taskArns"])
            tasks = []
            ordered = sorted(arns)
            for start in range(0, len(ordered), 100):
                result = self.ecs.describe_tasks(
                    cluster=CLUSTER, tasks=ordered[start : start + 100]
                )
                if result.get("failures"):
                    raise RuntimeError("Cannot prove task termination")
                tasks.extend(result["tasks"])
            if not unsettled(tasks, NAMES):
                return
            time.sleep(10)
        raise TimeoutError("Product worker is still active; paused release can be resumed")

    def apply_candidate(self, context: dict) -> None:
        # Pausing this stack invalidates the original change set. Recreate only its
        # frozen template and values, with admission disabled until it converges.
        from personal_apply import validate_changes

        planned = {p["ParameterKey"]: p["ParameterValue"] for p in context["parameters"]}
        planned["MetadataEnabled"] = "false"
        current = self.stack(RUNTIME)
        if current["StackStatus"].endswith("IN_PROGRESS"):
            self.wait_stack(RUNTIME)
            current = self.stack(RUNTIME)
        actual = {p["ParameterKey"]: p["ParameterValue"] for p in current["Parameters"]}
        guard_candidate_state(actual, context["previous"], planned)
        name = "release-" + self.operation + "-runtime"
        try:
            change = self.cf.describe_change_set(StackName=RUNTIME, ChangeSetName=name)
        except ClientError as exc:
            if "does not exist" not in str(exc):
                raise
            body = context["template"]
            result = self.cf.create_change_set(
                StackName=RUNTIME,
                ChangeSetName=name,
                ChangeSetType="UPDATE",
                TemplateBody=body if isinstance(body, str) else json.dumps(body),
                Parameters=[{"ParameterKey": k, "ParameterValue": v} for k, v in planned.items()],
                Capabilities=["CAPABILITY_NAMED_IAM"],
                RoleARN=ROLE,
                Description="frozen-reviewed-release:" + self.arn,
            )
            for _ in range(60):
                change = self.cf.describe_change_set(ChangeSetName=result["Id"])
                if change["Status"] not in {"CREATE_PENDING", "CREATE_IN_PROGRESS"}:
                    break
                time.sleep(5)
        if change["Status"] == "FAILED" and "didn't contain changes" in change.get(
            "StatusReason", ""
        ):
            if actual != planned:
                raise ValueError("Empty candidate differs from the reviewed parameters")
            return
        validate_changes(change)
        parameters = {p["ParameterKey"]: p["ParameterValue"] for p in change["Parameters"]}
        if parameters != planned:
            raise ValueError("Candidate changed after review")
        body = self.cf.get_template(StackName=RUNTIME, ChangeSetName=change["ChangeSetId"])[
            "TemplateBody"
        ]
        expected = context["template"]
        if isinstance(body, str):
            body = json.loads(body)
        if isinstance(expected, str):
            expected = json.loads(expected)
        if body != expected:
            raise ValueError("Candidate template differs from reviewed content")
        print(
            json.dumps(
                {
                    "reviewed_change_set": self.arn,
                    "paused_candidate_change_set": change["ChangeSetId"],
                }
            ),
            flush=True,
        )
        if change["ExecutionStatus"] == "AVAILABLE":
            self.cf.execute_change_set(ChangeSetName=change["ChangeSetId"])
            self.wait_stack(RUNTIME)
        elif change["ExecutionStatus"] != "EXECUTE_COMPLETE" or actual != planned:
            raise ValueError("Candidate execution needs investigation")

    def revisions(self) -> dict:
        # The runtime stack owns the metadata task and registration grant together.
        return {}

    def run(self) -> None:
        if self.read("complete"):
            print(json.dumps({"operation": self.operation, "status": "already_applied"}))
            return
        context = self.read("start")
        if context is None:
            state = self.stack(BACKGROUND)
            if state["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
                raise ValueError("Background stack must be stable before a release")
            enabled = next(
                p["ParameterValue"]
                for p in state["Parameters"]
                if p["ParameterKey"] == "MetadataEnabled"
            )
            original = self.cf.describe_change_set(ChangeSetName=self.arn)
            context = {
                "change_set": self.arn,
                "restore_enabled": enabled,
                "parameters": original["Parameters"],
                "previous": {p["ParameterKey"]: p["ParameterValue"] for p in state["Parameters"]},
                "template": self.cf.get_template(StackName=RUNTIME, ChangeSetName=self.arn)[
                    "TemplateBody"
                ],
            }
            self.record("start", context)
        if context["change_set"] != self.arn:
            raise ValueError("Wrong release checkpoint")
        if self.read("runtime") is None:
            self.change_background("pause", {"MetadataEnabled": "false"})
            self.wait_ack()
            self.wait_drained()
            self.record("drained", {"change_set": self.arn})
            self.apply_candidate(context)
            self.record("runtime", {"change_set": self.arn})
        if self.read("revisions") is None:
            revisions = self.revisions()
            self.change_background("revisions", revisions | {"MetadataEnabled": "false"})
            self.record("revisions", revisions)
        self.change_background("resume", {"MetadataEnabled": context["restore_enabled"]})
        self.wait_ack()
        self.record("complete", {"change_set": self.arn, "enabled": context["restore_enabled"]})
        print(json.dumps({"operation": self.operation, "status": "complete"}))
