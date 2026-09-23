"""Read-only production evidence with explicit traffic separation and redaction."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from analyze import percentile
from checks import require

COMPLETION_FIELDS = frozenset(
    {
        "event",
        "request_id",
        "static_work_id",
        "singleflight_joined",
        "retry_count",
        "scope",
        "outcome",
        "render_mode",
        "mime",
        "pagination",
        "cache_eligible",
        "cache_hit",
        "cache_key_id",
        "cache_entry_bytes",
        "cache_ttl_seconds",
        "duration_ms",
        "static_download_bytes",
        "source_document_bytes",
        "rendered_dom_bytes",
        "upstream_status",
        "phases_ms",
        "RequestCount",
        "Latency",
        "service",
        "transport",
    }
)
MEMORY_FIELDS = frozenset(
    {
        "MemoryWorkingSet",
        "MemoryAnon",
        "MemoryFile",
        "MemoryOOMKills",
        "MemoryAdmissionPaused",
        "MemorySampleFailure",
        "MemoryReclaimFailure",
        "MemoryIdleReclaim",
        "MemoryReservedBytes",
        "MemoryReservationRejected",
        "ParserRSS",
        "BrowserRSS",
        "ParserActive",
        "BrowserActive",
        "ParserStarts",
        "BrowserStarts",
        "ScratchReservedBytes",
        "DownloadActive",
        "ParseActive",
        "DownloadQueueDepth",
        "ParseQueueDepth",
        "BrowserQueueDepth",
        "DownloadQueueRejected",
        "ParseQueueRejected",
        "BrowserQueueRejected",
    }
)
ERROR_TERMS = (
    "Exception in ASGI application",
    "Task exception was never retrieved",
    "TargetClosedError",
    "OutOfMemoryError",
    "MemoryError",
)
SUCCESS = frozenset(
    {"initial_success", "pagination_success", "static_success", "browser_success", "cache_hit"}
)


def sanitize(envelope: dict, row: dict) -> dict:
    return {
        "timestamp": datetime.fromtimestamp(envelope["timestamp"] / 1000, UTC).isoformat(),
        "event_id": envelope["eventId"],
        "stream": envelope["logStreamName"],
        **{key: value for key, value in row.items() if key in COMPLETION_FIELDS | MEMORY_FIELDS},
    }


def outcomes(rows: list[dict]) -> dict:
    count = len(rows)
    successful = [r for r in rows if r.get("outcome") in SUCCESS]
    return {
        "requests": count,
        "outcomes": dict(Counter(r.get("outcome", "unknown") for r in rows)),
        "success_rate": len(successful) / count if count else None,
        "rejection_rate": sum(r.get("outcome") == "error_extract_capacity_exceeded" for r in rows)
        / count
        if count
        else None,
        "success_p50_ms": percentile(
            [r["duration_ms"] for r in successful if "duration_ms" in r], 0.5
        ),
        "success_p95_ms": percentile(
            [r["duration_ms"] for r in successful if "duration_ms" in r], 0.95
        ),
        "all_outcome_p95_ms": percentile(
            [r["duration_ms"] for r in rows if "duration_ms" in r], 0.95
        ),
    }


def completion_summary(rows: list[dict], canary_ids: set[str]) -> dict:
    natural = [r for r in rows if r.get("request_id") not in canary_ids]
    public = [r for r in natural if r.get("scope") in {"rest", "mcp"}]
    initial = [r for r in public if not r.get("pagination", False)]
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in initial:
        groups[f"{row.get('mime', 'unknown')}|{row.get('render_mode', 'unknown')}"].append(row)
    return {
        "natural_public_initial": outcomes(initial),
        "natural_public_pagination": outcomes([r for r in public if r.get("pagination")]),
        "natural_internal": outcomes([r for r in natural if r.get("scope") == "internal"]),
        "canary": outcomes(
            [
                r
                for r in rows
                if r.get("request_id") in canary_ids and r.get("scope") in {"rest", "mcp"}
            ]
        ),
        "natural_initial_by_mime_render": {
            key: outcomes(value) for key, value in sorted(groups.items())
        },
        "unexpected_completions": sum(r.get("outcome") == "unexpected_error" for r in rows),
    }


def fetch(logs, group: str, start: datetime, end: datetime, pattern: str):
    for page in logs.get_paginator("filter_log_events").paginate(
        logGroupName=group,
        startTime=int(start.timestamp() * 1000),
        endTime=int(end.timestamp() * 1000),
        filterPattern=pattern,
    ):
        yield from page.get("events", [])


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def collect(args) -> None:
    import boto3

    start, end = datetime.fromisoformat(args.start), datetime.fromisoformat(args.end)
    require(
        start.tzinfo is not None and end.tzinfo is not None, "Window must have explicit time zones"
    )
    require(start < end, "Observation window must be positive")
    require(not args.output.exists(), "Preserve previous observation evidence")
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    require(
        session.client("sts").get_caller_identity()["Account"] == args.expected_account,
        "AWS account does not match the reviewed observation target",
    )
    args.output.mkdir(parents=True)
    (args.output / "window.json").write_text(
        json.dumps(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "collected_at": datetime.now(UTC).isoformat(),
                "account": args.expected_account,
                "region": args.region,
                "cluster": args.cluster,
                "complete": False,
            },
            indent=2,
        )
    )
    logs, ecs = session.client("logs"), session.client("ecs")
    completions, memory, errors, legacy = [], [], [], []
    for component in ("api", "extract"):
        group = args.log_prefix.rstrip("/") + "/" + component
        for envelope in fetch(logs, group, start, end, '{ $.event = "extract_completed" }'):
            completions.append(sanitize(envelope, json.loads(envelope["message"])))
        if component == "extract":
            pattern = "{ " + " || ".join(f"$.{key} = *" for key in sorted(MEMORY_FIELDS)) + " }"
            for envelope in fetch(logs, group, start, end, pattern):
                memory.append(sanitize(envelope, json.loads(envelope["message"])))
            for envelope in fetch(logs, group, start, end, "{ $.RequestCount = * }"):
                legacy.append(sanitize(envelope, json.loads(envelope["message"])))
        for term in ERROR_TERMS:
            for envelope in fetch(logs, group, start, end, '"' + term + '"'):
                errors.append({"component": component, "kind": term, **sanitize(envelope, {})})
    write_jsonl(args.output / "completion.jsonl", completions)
    write_jsonl(args.output / "memory.jsonl", memory)
    write_jsonl(args.output / "lifecycle-errors.jsonl", errors)
    write_jsonl(args.output / "legacy-metrics.jsonl", legacy)
    services = ecs.describe_services(cluster=args.cluster, services=[args.service])
    require(not services.get("failures"), "ECS service observation failed")
    resources = []
    for service in services["services"]:
        definition = ecs.describe_task_definition(taskDefinition=service["taskDefinition"])[
            "taskDefinition"
        ]
        resources.append(
            {
                "service": service["serviceName"],
                "desired_count": service["desiredCount"],
                "task_definition": definition["taskDefinitionArn"],
                "containers": [
                    {
                        **{
                            key: c[key]
                            for key in ("name", "image", "cpu", "memory", "memoryReservation")
                            if key in c
                        },
                        "extract_concurrency": {
                            v["name"]: v["value"]
                            for v in c.get("environment", [])
                            if v["name"]
                            in {
                                "SCHOLIGHT_EXTRACT_STATIC_CONCURRENCY",
                                "SCHOLIGHT_EXTRACT_BROWSER_CONCURRENCY",
                            }
                        },
                    }
                    for c in definition["containerDefinitions"]
                ],
            }
        )
    (args.output / "resources.json").write_text(json.dumps(resources, indent=2))
    tasks = []
    for desired in ("RUNNING", "STOPPED"):
        for page in ecs.get_paginator("list_tasks").paginate(
            cluster=args.cluster,
            serviceName=args.service,
            desiredStatus=desired,
        ):
            arns = page["taskArns"]
            if not arns:
                continue
            response = ecs.describe_tasks(cluster=args.cluster, tasks=arns)
            require(not response.get("failures"), "ECS task observation failed")
            for task in response["tasks"]:
                tasks.append(
                    {
                        key: task[key]
                        for key in (
                            "taskArn",
                            "taskDefinitionArn",
                            "lastStatus",
                            "startedAt",
                            "stoppedAt",
                            "stoppedReason",
                            "stopCode",
                        )
                        if key in task
                    }
                    | {
                        "containers": [
                            {
                                key: c[key]
                                for key in (
                                    "name",
                                    "image",
                                    "imageDigest",
                                    "lastStatus",
                                    "exitCode",
                                    "reason",
                                )
                                if key in c
                            }
                            for c in task.get("containers", [])
                        ]
                    }
                )
    (args.output / "tasks.json").write_text(json.dumps(tasks, default=str, indent=2))
    (args.output / "service.json").write_text(
        json.dumps(
            [
                {
                    key: s[key]
                    for key in (
                        "serviceName",
                        "taskDefinition",
                        "desiredCount",
                        "runningCount",
                        "pendingCount",
                        "deployments",
                        "events",
                    )
                    if key in s
                }
                for s in services["services"]
            ],
            default=str,
            indent=2,
        )
    )
    canary_ids = set()
    for path in args.canary_report:
        canary_ids.update(
            row["request_id"]
            for row in json.loads(path.read_text())["records"]
            if row.get("request_id")
        )
    report = completion_summary(completions, canary_ids)
    report.update(
        {
            "complete": True,
            "completion_counting_supported": bool(memory),
            "lifecycle_error_matches": len(errors),
            "memory_samples": sum("MemoryWorkingSet" in row for row in memory),
            "peak_working_set_bytes": max(
                (r["MemoryWorkingSet"] for r in memory if "MemoryWorkingSet" in r), default=None
            ),
            "cgroup_oom_kills_by_stream": {
                stream: max(
                    r["MemoryOOMKills"]
                    for r in memory
                    if r["stream"] == stream and "MemoryOOMKills" in r
                )
                for stream in {r["stream"] for r in memory if "MemoryOOMKills" in r}
            },
            "memory_log_streams": sorted({r["stream"] for r in memory}),
            "limitations": [
                "STOPPED task retention is at least one hour; retain hourly snapshots and investigate stream/task changes.",
                "No completion or memory events before A means unavailable evidence, not zero activity.",
                "This window summary is not the multi-day acceptance gate.",
            ],
        }
    )
    (args.output / "summary.json").write_text(json.dumps(report, indent=2))
    window = json.loads((args.output / "window.json").read_text())
    window["complete"] = True
    (args.output / "window.json").write_text(json.dumps(window, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--log-prefix", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--canary-report", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    collect(parser.parse_args())
