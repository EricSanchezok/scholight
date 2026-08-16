"""Scholight Survey worker operations."""

from __future__ import annotations

import asyncio
import json
import os
import re

# Only a fixed, image-owned executable is invoked below.
import subprocess  # nosec B404
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import click

from scholight.config import (
    settings,
    validate_survey_draft_worker_settings,
    validate_survey_worker_settings,
)
from scholight.db.client import close_pool, create_pool, get_pool
from scholight.db.queries_survey import get_survey_job_counts
from scholight.db.queries_survey_cleanup import get_artifact_cleanup_status
from scholight.db.queries_survey_notifications import get_email_notification_status
from scholight.logging import configure_logging
from scholight.logging.emf import emit_emf
from scholight.survey.process import classify_rcm_error
from scholight.survey.runtime import image_canary_environment
from scholight.survey.worker import RCM_VERSION, serve_survey_worker
from scholight.survey.workflow_audit import workflow_audit_payload

_DIAGNOSTIC_JSON_MAX_BYTES = 2 * 1024 * 1024
_IMAGE_CANARY_FIELD = re.compile(
    r"\b(code|http_status|retryable|provider_code)=([A-Za-z0-9_.-]{1,128})\b"
)


def _echo_concurrency(concurrency: object) -> None:
    if not isinstance(concurrency, dict):
        raise click.ClickException("Survey concurrency response is invalid")
    for queue in ("draft", "survey"):
        limits = concurrency.get(queue)
        if not isinstance(limits, dict):
            raise click.ClickException("Survey concurrency response is invalid")
        click.echo(
            f"{queue} concurrency: global={limits.get('global')}, "
            f"per-user={limits.get('per_user')}, per-worker={limits.get('per_worker')}"
        )


def _installed_rcm_version() -> str:
    # The command and arguments are constants installed in the reviewed image.
    completed = subprocess.run(  # nosec B603
        ["/usr/local/bin/accelerate", "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    output = (completed.stdout or completed.stderr).strip()
    version = output.rsplit(maxsplit=1)[-1].removeprefix("v") if output else ""
    if version != RCM_VERSION:
        raise RuntimeError("Installed RCM version does not match the reviewed release")
    return version


def _run_image_canary() -> dict[str, object]:
    """Run the fixed RCM canary and return only bounded diagnostic fields."""
    started_at = time.perf_counter()
    with tempfile.TemporaryDirectory(
        prefix=".survey-image-canary-",
        dir=settings.data_root,
    ) as directory:
        output = Path(directory) / "canary.png"
        try:
            completed = subprocess.run(  # nosec B603
                [
                    "/usr/local/bin/accelerate",
                    "image-canary",
                    "--output",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=13 * 60,
                env=image_canary_environment(),
            )
        except subprocess.TimeoutExpired:
            completed = None
        duration_ms = max(0, round((time.perf_counter() - started_at) * 1000))
        if completed is None:
            result: dict[str, object] = {
                "schema_version": 1,
                "status": "failed",
                "error_code": "image_canary_timeout",
                "http_status": None,
                "retryable": True,
                "provider_code": "timeout",
                "duration_ms": duration_ms,
                "size": None,
            }
        elif completed.returncode == 0:
            try:
                payload = json.loads(completed.stdout)
                size = output.stat().st_size
            except (OSError, json.JSONDecodeError):
                payload = None
                size = 0
            if not (
                isinstance(payload, dict)
                and payload.get("status") == "ok"
                and size > 0
                and payload.get("size") == size
            ):
                result = {
                    "schema_version": 1,
                    "status": "failed",
                    "error_code": "image_canary_invalid_result",
                    "http_status": None,
                    "retryable": False,
                    "provider_code": "invalid_success_result",
                    "duration_ms": duration_ms,
                    "size": None,
                }
            else:
                result = {
                    "schema_version": 1,
                    "status": "ok",
                    "error_code": None,
                    "http_status": None,
                    "retryable": None,
                    "provider_code": None,
                    "duration_ms": duration_ms,
                    "size": size,
                }
        else:
            fields = dict(_IMAGE_CANARY_FIELD.findall(completed.stderr))
            result = {
                "schema_version": 1,
                "status": "failed",
                "error_code": fields.get("code", "image_canary_failed"),
                "http_status": (
                    int(fields["http_status"]) if fields.get("http_status", "").isdigit() else None
                ),
                "retryable": (fields["retryable"] == "true" if "retryable" in fields else None),
                "provider_code": fields.get("provider_code"),
                "duration_ms": duration_ms,
                "size": None,
            }
    emit_emf(
        service="survey-image-canary",
        outcome=str(result["status"]),
        metrics={"SurveyImageCanaryCount": (1, "Count")},
    )
    return result


def _verify_diagnostic_workspace(data_root: Path) -> None:
    """Confirm the worker can atomically persist private diagnostics on its data volume."""
    with tempfile.TemporaryDirectory(
        prefix=".survey-diagnostics-smoke-", dir=data_root
    ) as directory:
        root = Path(directory)
        path = root / "diagnostics.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(b'{"ok":true}')
            handle.flush()
            os.fsync(handle.fileno())
        if path.read_bytes() != b'{"ok":true}':
            raise RuntimeError("Survey diagnostic workspace verification failed")


async def _verify_survey_runtime_schema() -> None:
    """Probe the current Survey schema through application-visible columns."""
    await get_pool().fetch(
        "SELECT surveys.title, surveys.notify_on_completion, drafts.request_hash, "
        "jobs.cancel_requested_at, cleanup.status, notifications.status "
        "FROM scholight.surveys AS surveys "
        "CROSS JOIN scholight.survey_drafts AS drafts "
        "CROSS JOIN scholight.survey_jobs AS jobs "
        "CROSS JOIN scholight.survey_artifact_cleanup_outbox AS cleanup "
        "CROSS JOIN scholight.survey_email_notifications AS notifications LIMIT 0"
    )


@click.group("survey")
def survey_group() -> None:
    """Run and inspect durable Scholight Survey jobs."""


@survey_group.command("serve-worker")
def serve_worker() -> None:
    """Run the concurrent Survey execution and archive supervisor."""
    configure_logging()
    try:
        validate_survey_worker_settings()
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    async def _run() -> None:
        await create_pool()
        try:
            await serve_survey_worker()
        finally:
            await close_pool()

    asyncio.run(_run())


@survey_group.command("serve-draft-worker")
def serve_draft_worker() -> None:
    """Run the independent concurrent Survey Draft supervisor."""
    configure_logging()
    try:
        validate_survey_draft_worker_settings()
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    async def _run() -> None:
        from scholight.survey.draft_worker import serve_survey_draft_worker

        await create_pool()
        try:
            await serve_survey_draft_worker()
        finally:
            await close_pool()

    asyncio.run(_run())


@survey_group.command("status")
@click.option("--json-output", "json_output", is_flag=True, help="Emit machine-readable JSON.")
def status(json_output: bool) -> None:
    """Show queue counts without starting a worker."""
    configure_logging()

    async def _read() -> dict[str, object]:
        await create_pool()
        try:
            jobs = await get_survey_job_counts()
            cleanup = await get_artifact_cleanup_status()
            email = await get_email_notification_status()
            oldest_age = (
                max(0, int((datetime.now(UTC) - cleanup.oldest_waiting_at).total_seconds()))
                if cleanup.oldest_waiting_at is not None
                else None
            )
            return {
                "rcm_version": RCM_VERSION,
                "jobs": jobs,
                "cleanup": {
                    "pending": cleanup.pending,
                    "running": cleanup.running,
                    "retry": cleanup.retry,
                    "succeeded": cleanup.succeeded,
                    "dead": cleanup.dead,
                    "oldest_waiting_seconds": oldest_age,
                },
                "email_notifications": {
                    "pending": email.pending,
                    "running": email.running,
                    "retry": email.retry,
                    "succeeded": email.succeeded,
                    "dead": email.dead,
                    "oldest_waiting_seconds": (
                        max(
                            0,
                            int((datetime.now(UTC) - email.oldest_waiting_at).total_seconds()),
                        )
                        if email.oldest_waiting_at is not None
                        else None
                    ),
                },
                "concurrency": {
                    "draft": {
                        "global": settings.survey_draft_global_concurrency,
                        "per_user": settings.survey_draft_per_user_concurrency,
                        "per_worker": settings.survey_draft_worker_concurrency,
                    },
                    "survey": {
                        "global": settings.survey_job_global_concurrency,
                        "per_user": settings.survey_job_per_user_concurrency,
                        "per_worker": settings.survey_job_worker_concurrency,
                    },
                },
            }
        finally:
            await close_pool()

    payload = asyncio.run(_read())
    if json_output:
        click.echo(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return
    click.echo(f"RCM expected version: {RCM_VERSION}")
    jobs = payload["jobs"]
    if not isinstance(jobs, dict):
        raise click.ClickException("Survey status response is invalid")
    for state, count in jobs.items():
        click.echo(f"{state}: {count}")
    _echo_concurrency(payload["concurrency"])


@survey_group.command("contract-audit")
@click.option("--json-output", "json_output", is_flag=True, help="Emit machine-readable JSON.")
def contract_audit(json_output: bool) -> None:
    """Report known workflow definition conflicts without changing the workflow."""
    payload = workflow_audit_payload()
    if json_output:
        click.echo(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return
    click.echo(f"Workflow contract status: {payload['status']}")
    conflicts = payload["conflicts"]
    if not isinstance(conflicts, list):
        raise click.ClickException("Workflow contract audit response is invalid")
    for conflict in conflicts:
        if isinstance(conflict, dict):
            click.echo(f"- {conflict.get('code')}: {conflict.get('summary')}")


def _read_local_diagnostics(job_id: UUID) -> tuple[dict[str, Any], Path] | None:
    run_root = Path(settings.data_root) / "surveys" / str(job_id) / "run"
    for name in ("diagnostics.json", "run.json"):
        path = run_root / name
        try:
            if (
                not path.is_file()
                or path.is_symlink()
                or path.stat().st_size > _DIAGNOSTIC_JSON_MAX_BYTES
            ):
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and str(payload.get("job_id")) == str(job_id):
            return payload, path
    return None


async def _read_stream_json(stream: Any) -> dict[str, Any]:
    body = bytearray()
    async for chunk in stream.chunks():
        body.extend(chunk)
        if len(body) > _DIAGNOSTIC_JSON_MAX_BYTES:
            raise RuntimeError("Survey diagnostic artifact is too large")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Survey diagnostic artifact is invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Survey diagnostic artifact is invalid")
    return payload


def _diagnostic_projection(
    payload: dict[str, Any],
    *,
    job_id: UUID,
    source: str,
    location: str,
    database: dict[str, object] | None = None,
) -> dict[str, object]:
    nested = payload.get("diagnostics")
    diagnostics = nested if isinstance(nested, dict) else payload
    process = payload.get("process") if isinstance(payload.get("process"), dict) else None
    stderr_classification = None
    if process is not None and isinstance(process.get("stderr_tail"), str):
        error_code, error_message = classify_rcm_error(process["stderr_tail"])
        stderr_classification = {"code": error_code, "message": error_message}
    return {
        "schema_version": 1,
        "job_id": str(job_id),
        "source": source,
        "location": location,
        "status": (database or {}).get("status"),
        "outcome": payload.get("outcome") or (database or {}).get("terminal_outcome"),
        "error_code": payload.get("error_code") or (database or {}).get("error_code"),
        "process": process,
        "last_successful_component": diagnostics.get("last_successful_component"),
        "first_anomaly": diagnostics.get("first_anomaly"),
        "affected_components": diagnostics.get("affected_components", []),
        "anomaly_count": diagnostics.get("anomaly_count", 0),
        "tool_counts": diagnostics.get("tool_counts", {}),
        "model_counts": diagnostics.get("model_counts", {}),
        "last_model_error": diagnostics.get("last_model_error"),
        "stderr_classification": stderr_classification,
        "last_activity_at": diagnostics.get("last_activity_at"),
        "trace_path": diagnostics.get("trace_path"),
        "manifest_key": (database or {}).get("manifest_key"),
    }


async def _diagnose_archived(job_id: UUID) -> dict[str, object]:
    from scholight.survey.artifacts import SurveyArtifactStore

    await create_pool()
    try:
        row = await get_pool().fetchrow(
            "SELECT j.status, j.terminal_outcome, j.error_code, j.storage_bucket, "
            "j.manifest_key FROM scholight.survey_jobs j WHERE j.id = $1",
            job_id,
        )
        if row is None:
            raise RuntimeError("Survey job was not found")
        database = {
            "status": str(row["status"]),
            "terminal_outcome": row["terminal_outcome"],
            "error_code": row["error_code"],
            "manifest_key": row["manifest_key"],
        }
        manifest_key = row["manifest_key"]
        if not isinstance(manifest_key, str) or not manifest_key:
            return _diagnostic_projection(
                {},
                job_id=job_id,
                source="database",
                location="survey_jobs",
                database=database,
            )
        bucket = str(row["storage_bucket"] or settings.survey_s3_bucket)
        store = SurveyArtifactStore(
            bucket=bucket,
            endpoint_url=settings.survey_s3_endpoint_url,
        )
        stream = await store.open_artifact(manifest_key=manifest_key, path="run.json")
        payload = await _read_stream_json(stream)
        return _diagnostic_projection(
            payload,
            job_id=job_id,
            source="archive",
            location=f"s3://{bucket}/{manifest_key}",
            database=database,
        )
    finally:
        await close_pool()


@survey_group.command("diagnose")
@click.argument("job_id", type=str)
@click.option("--json-output", "json_output", is_flag=True, help="Emit machine-readable JSON.")
def diagnose(job_id: str, json_output: bool) -> None:
    """Read one active or archived Survey diagnostic summary without rerunning it."""
    configure_logging()
    try:
        parsed_job_id = UUID(job_id)
    except ValueError as exc:
        raise click.ClickException("Survey job id must be a UUID") from exc
    local = _read_local_diagnostics(parsed_job_id)
    try:
        payload = (
            _diagnostic_projection(
                local[0],
                job_id=parsed_job_id,
                source="workspace",
                location=str(local[1]),
            )
            if local is not None
            else asyncio.run(_diagnose_archived(parsed_job_id))
        )
    except Exception as exc:
        raise click.ClickException("Survey diagnostics could not be read") from exc
    if json_output:
        click.echo(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return
    click.echo(f"Survey job: {payload['job_id']}")
    click.echo(f"Source: {payload['source']} ({payload['location']})")
    click.echo(f"Outcome: {payload['outcome'] or payload['status'] or 'running'}")
    process = payload["process"]
    if isinstance(process, dict):
        click.echo(
            "Process: "
            f"return_code={process.get('return_code')} "
            f"termination_reason={process.get('termination_reason') or 'unknown'}"
        )
    click.echo(f"Last successful component: {payload['last_successful_component'] or 'unknown'}")
    first_anomaly = payload["first_anomaly"]
    if isinstance(first_anomaly, dict):
        click.echo(
            "First anomaly: "
            f"{first_anomaly.get('component')} missing "
            f"{first_anomaly.get('expected_artifact')}"
        )
    else:
        click.echo("First anomaly: none observed")
    affected = payload["affected_components"]
    affected_items = affected if isinstance(affected, list) else []
    click.echo(
        "Affected components: "
        + (", ".join(str(component) for component in affected_items) if affected_items else "none")
    )
    tool_counts = payload["tool_counts"]
    normalized_tool_counts = tool_counts if isinstance(tool_counts, dict) else {}
    click.echo(
        "Tool calls: "
        f"started={normalized_tool_counts.get('started', 0)} "
        f"finished={normalized_tool_counts.get('finished', 0)} "
        f"failed={normalized_tool_counts.get('failed', 0)}"
    )
    model_counts = payload["model_counts"]
    normalized_model_counts = model_counts if isinstance(model_counts, dict) else {}
    click.echo(
        "Model calls: "
        f"started={normalized_model_counts.get('started', 0)} "
        f"finished={normalized_model_counts.get('finished', 0)} "
        f"failed={normalized_model_counts.get('failed', 0)}"
    )
    last_model_error = payload["last_model_error"]
    click.echo(
        "Last model error: "
        + (
            str(last_model_error.get("error_code", "unknown"))
            if isinstance(last_model_error, dict)
            else "none"
        )
    )
    stderr_classification = payload["stderr_classification"]
    click.echo(
        "Stderr classification: "
        + (
            str(stderr_classification.get("code", "unknown"))
            if isinstance(stderr_classification, dict)
            else "none"
        )
    )
    click.echo(f"Trace: {payload['trace_path'] or 'unavailable'}")
    click.echo(f"Archive: {payload['manifest_key'] or payload['location']}")


@survey_group.command("recover-archived")
@click.argument("job_id", type=str)
@click.option("--apply", "apply_recovery", is_flag=True, help="Apply the verified transition.")
@click.option(
    "--expected-source-manifest-sha256",
    type=str,
    help="Required immutable source manifest guard when --apply is used.",
)
@click.option(
    "--expected-report-sha256",
    type=str,
    help="Required immutable report guard when --apply is used.",
)
@click.option("--json-output", "json_output", is_flag=True, help="Emit machine-readable JSON.")
def recover_archived(
    job_id: str,
    apply_recovery: bool,
    expected_source_manifest_sha256: str | None,
    expected_report_sha256: str | None,
    json_output: bool,
) -> None:
    """Verify, then optionally recover, one archived contract failure in place."""
    from scholight.survey.recovery import recover_archived_survey

    configure_logging()
    try:
        parsed_job_id = UUID(job_id)
    except ValueError as exc:
        raise click.ClickException("Survey job id must be a UUID") from exc

    async def _run() -> dict[str, object]:
        await create_pool()
        try:
            result = await recover_archived_survey(
                job_id=parsed_job_id,
                apply=apply_recovery,
                expected_source_manifest_sha256=expected_source_manifest_sha256,
                expected_report_sha256=expected_report_sha256,
            )
            return result.as_dict()
        finally:
            await close_pool()

    try:
        payload = asyncio.run(_run())
    except Exception as exc:
        raise click.ClickException("Archived Survey recovery verification failed") from exc
    if json_output:
        click.echo(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return
    action = "applied" if payload["applied"] else "dry-run verified"
    click.echo(f"Archived Survey recovery {action}.")
    click.echo(f"Recovery type: {payload['recovery_type']}")
    click.echo(f"Source manifest SHA256: {payload['source_manifest_sha256']}")
    click.echo(f"Report SHA256: {payload['report_sha256']}")
    click.echo(f"Expected manifest: {payload['manifest_key']}")


@survey_group.command("image-canary")
@click.option("--json-output", "json_output", is_flag=True, help="Emit machine-readable JSON.")
def image_canary(json_output: bool) -> None:
    """Generate one fixed, non-sensitive image and report sanitized diagnostics."""
    configure_logging()
    try:
        payload = _run_image_canary()
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        raise click.ClickException("Image canary could not be executed") from exc
    if json_output:
        click.echo(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    else:
        click.echo(f"Image canary: {payload['status']}")
        click.echo(f"Error code: {payload['error_code'] or 'none'}")
        click.echo(f"HTTP status: {payload['http_status'] or 'none'}")
        click.echo(f"Retryable: {payload['retryable']}")
        click.echo(f"Provider code: {payload['provider_code'] or 'none'}")
        click.echo(f"Duration: {payload['duration_ms']} ms")
    if payload["status"] != "ok":
        raise click.exceptions.Exit(1)


@survey_group.command("smoke")
@click.option("--json-output", "json_output", is_flag=True, help="Emit machine-readable JSON.")
def smoke(json_output: bool) -> None:
    """Validate runtime schema, cleanup health, RCM identity, and artifact access."""
    configure_logging()
    try:
        validate_survey_worker_settings()
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    async def _run() -> dict[str, object]:
        from scholight.survey.artifacts import SurveyArtifactStore

        installed_rcm_version = await asyncio.to_thread(_installed_rcm_version)
        await asyncio.to_thread(_verify_diagnostic_workspace, Path(settings.data_root))
        await create_pool()
        try:
            await _verify_survey_runtime_schema()
            cleanup = await get_artifact_cleanup_status()
            if cleanup.dead:
                raise RuntimeError("Survey artifact cleanup has dead tasks")
            store = SurveyArtifactStore(
                bucket=settings.survey_s3_bucket,
                endpoint_url=settings.survey_s3_endpoint_url,
            )
            await store.verify_access()
            return {
                "ok": True,
                "rcm_version": installed_rcm_version,
                "runtime_schema": "compatible",
                "cleanup_dead": cleanup.dead,
                "diagnostics_writable": True,
                "workflow_contract": workflow_audit_payload(),
                "concurrency": {
                    "draft": {
                        "global": settings.survey_draft_global_concurrency,
                        "per_user": settings.survey_draft_per_user_concurrency,
                        "per_worker": settings.survey_draft_worker_concurrency,
                    },
                    "survey": {
                        "global": settings.survey_job_global_concurrency,
                        "per_user": settings.survey_job_per_user_concurrency,
                        "per_worker": settings.survey_job_worker_concurrency,
                    },
                },
            }
        finally:
            await close_pool()

    try:
        payload = asyncio.run(_run())
    except Exception as exc:
        raise click.ClickException("Survey production smoke checks did not pass") from exc
    if json_output:
        click.echo(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    else:
        click.echo("Survey production smoke checks passed.")
        _echo_concurrency(payload["concurrency"])


__all__ = ["survey_group"]
