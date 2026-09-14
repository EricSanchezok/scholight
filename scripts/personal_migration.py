"""Apply a reviewed append using a pinned candidate image and private migrator role."""

from __future__ import annotations

import json
import time

from botocore.exceptions import ClientError
from personal_compatibility import contract, receipt_key, verify_receipt
from release_admission import (
    ACCOUNT,
    BUCKET,
    CLUSTER,
    REGION,
    RUNTIME,
    AdmissionRelease,
    pause_values,
)


def candidate_definition(current: dict, manifest: dict) -> dict:
    if (
        current["family"] != "scholight-personal-migration"
        or len(current["containerDefinitions"]) != 1
    ):
        raise ValueError("Unexpected private migrator definition")
    for key, suffix in (("taskRoleArn", "Task"), ("executionRoleArn", "Execution")):
        if current[key] != f"arn:aws:iam::{ACCOUNT}:role/ScholightPersonalMigration{suffix}":
            raise ValueError("Migration must retain the dedicated existing product roles")
    if current.get("runtimePlatform", {}).get("cpuArchitecture") != "ARM64":
        raise ValueError("Migration requires native ARM64")
    # Copy only registerable fields. Network, secrets, roles and logging stay private.
    result = {
        key: current[key]
        for key in (
            "family",
            "taskRoleArn",
            "executionRoleArn",
            "networkMode",
            "containerDefinitions",
            "volumes",
            "placementConstraints",
            "requiresCompatibilities",
            "cpu",
            "memory",
            "runtimePlatform",
        )
        if key in current
    }
    result = json.loads(json.dumps(result))
    container = result["containerDefinitions"][0]
    container["image"] = manifest["images"]["api"]
    container["command"] = ["scholight", "store", "migrate"]
    result["tags"] = [{"key": "scholight-operation", "value": "compatibility-migration"}]
    return result


def execute(manifest: dict, manifest_digest: str) -> None:
    from personal_apply import compatible_with_running

    operation = "migration-" + manifest_digest[:32]
    release = AdmissionRelease(
        f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:changeSet/manual/{operation}"
    )
    state = release.stack(RUNTIME)
    parameters = {p["ParameterKey"]: p["ParameterValue"] for p in state["Parameters"]}
    compatible_with_running(parameters, manifest)
    key = receipt_key(manifest)
    try:
        previous = json.loads(release.s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchKey":
            raise
    else:
        verify_receipt(previous, parameters, manifest)
        print(json.dumps({"status": "already_applied", "receipt_key": key}))
        return
    context = release.read("migration-start")
    if context is None:
        if state["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
            raise ValueError("Runtime must be stable before private migration")
        context = {
            "manifest_sha256": manifest_digest,
            "database_secret": parameters["DatabaseMigratorSecretArn"],
            "previous_admission": {k: parameters[k] for k in pause_values(parameters)},
        }
        release.record("migration-start", context)
    if (
        context["manifest_sha256"] != manifest_digest
        or context["database_secret"] != parameters["DatabaseMigratorSecretArn"]
    ):
        raise ValueError("Migration continuation no longer matches its reviewed destination")
    release.change_background("migration-pause", pause_values(parameters))
    release.wait_ack()
    release.wait_drained()
    outputs = {v["OutputKey"]: v["OutputValue"] for v in state["Outputs"]}
    definition = release.ecs.describe_task_definition(
        taskDefinition=outputs["MigrationTaskDefinitionArn"]
    )["taskDefinition"]
    task = release.read("migration-task")
    if task is None:
        intent = release.read("migration-launch")
        if intent is None:
            registered = release.ecs.register_task_definition(
                **candidate_definition(definition, manifest)
            )["taskDefinition"]["taskDefinitionArn"]
            intent = {"definition": registered, "created_at": time.time()}
            release.record("migration-launch", intent)
        if not 0 <= time.time() - intent["created_at"] <= 3600:
            raise ValueError(
                "Unconfirmed launch exceeded its idempotency window; investigate before retry"
            )
        registered = intent["definition"]
        response = release.ecs.run_task(
            cluster=CLUSTER,
            launchType="EC2",
            taskDefinition=registered,
            count=1,
            startedBy="reviewed-product-migration",
            clientToken=manifest_digest,
        )
        if response.get("failures") or len(response.get("tasks", [])) != 1:
            release.ecs.deregister_task_definition(taskDefinition=registered)
            raise RuntimeError("Candidate migration did not start; consumers remain paused")
        task = {"task": response["tasks"][0]["taskArn"], "definition": registered}
        release.record("migration-task", task)
    print(
        json.dumps({"migration_task": task["task"], "image": manifest["images"]["api"]}), flush=True
    )
    for _ in range(120):
        response = release.ecs.describe_tasks(cluster=CLUSTER, tasks=[task["task"]])
        if response.get("failures") or len(response.get("tasks", [])) != 1:
            raise RuntimeError("Cannot prove candidate migration outcome")
        description = response["tasks"][0]
        if description["lastStatus"] == "STOPPED":
            if not description.get("containers") or any(
                c.get("exitCode") != 0 for c in description["containers"]
            ):
                raise RuntimeError(
                    "Migration failed; admission stays paused and no proof is emitted"
                )
            receipt = {
                "status": "complete",
                "account": ACCOUNT,
                "region": REGION,
                "cluster": CLUSTER,
                "contract": contract(manifest),
                "database_secret": context["database_secret"],
                "image": manifest["images"]["api"],
                "task": task["task"],
                "completed_at": time.time(),
            }
            release.s3.put_object(
                Bucket=BUCKET,
                Key=key,
                Body=json.dumps(receipt, sort_keys=True).encode(),
                ContentType="application/json",
                IfNoneMatch="*",
            )
            release.ecs.deregister_task_definition(taskDefinition=task["definition"])
            print(json.dumps({"status": "complete", "receipt_key": key, "admission": "paused"}))
            return
        time.sleep(10)
    raise TimeoutError("Migration is still active; admission remains paused, resume this operation")
