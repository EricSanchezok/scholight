"""Bounded production operations for owner-preserving Survey reruns.

This module is intentionally not exposed through the public API.  It is executed by a
fixed production-environment workflow inside the deployed API task definition.  Inputs
are UUIDs, the source owner is copied from the database, and output contains no request
or paper content.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from scholight.config import settings
from scholight.db.client import close_pool, create_pool, get_pool
from scholight.db.queries_survey import create_survey, start_survey
from scholight.survey.contracts import canonical_request_hash
from scholight.survey.quality_repair import (
    apply_archived_evidence_repair,
    inspect_archived_evidence_repair,
)

_TERMINAL_SURVEY_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_FORBIDDEN_REPORT_MARKERS = (
    "pdftotext unavailable",
    "pdftotext not installed",
    "run metadata",
    "papercard",
    "本环境缺少 pdftotext",
    "运行环境缺少 pdftotext",
)


class ProductionSurveyOperationError(RuntimeError):
    """A bounded production operation failed without exposing research content."""


class ProductionSurveyAcceptanceError(ProductionSurveyOperationError):
    """A terminal Survey did not satisfy the production acceptance contract."""


@dataclass(frozen=True, slots=True)
class RerunIdentifiers:
    survey_id: UUID
    draft_id: UUID
    job_id: UUID
    start_request_id: UUID


def rerun_identifiers(operation_id: UUID) -> RerunIdentifiers:
    """Derive stable aggregate identifiers so workflow retries remain idempotent."""
    prefix = f"scholight:production-survey-rerun:{operation_id}"
    return RerunIdentifiers(
        survey_id=uuid5(NAMESPACE_URL, f"{prefix}:survey"),
        draft_id=uuid5(NAMESPACE_URL, f"{prefix}:draft"),
        job_id=uuid5(NAMESPACE_URL, f"{prefix}:job"),
        start_request_id=uuid5(NAMESPACE_URL, f"{prefix}:start"),
    )


def acceptance_payload(
    *,
    survey_id: UUID,
    job_id: UUID,
    survey_status: str,
    job_status: str,
    terminal_outcome: str | None,
    error_code: str | None,
    manifest_sha256: str,
    report_sha256: str,
    package_sha256: str,
    card_count: int,
    section_count: int,
    coverage_percent: float,
    notification_count: int,
    notification_status: str | None,
    minimum_coverage: float = 80.0,
    quota_state: str = "consumed",
    unknown_count: int = 0,
    invalid_reason_count: int = 0,
    runtime_marker_count: int = 0,
) -> dict[str, object]:
    """Validate and project only non-sensitive production acceptance evidence."""
    if survey_status != "succeeded" or job_status != "finished" or terminal_outcome != "succeeded":
        suffix = f" ({error_code})" if error_code else ""
        raise ProductionSurveyAcceptanceError(f"Survey did not succeed{suffix}")
    if error_code is not None:
        raise ProductionSurveyAcceptanceError("Survey quality checks did not complete cleanly")
    if quota_state != "consumed":
        raise ProductionSurveyAcceptanceError(
            "Survey allowance was not consumed by the clean rerun"
        )
    if card_count <= 0 or section_count <= 0:
        raise ProductionSurveyAcceptanceError("Survey required artifacts are incomplete")
    if coverage_percent < minimum_coverage:
        raise ProductionSurveyAcceptanceError(
            f"Survey full-text coverage is below {minimum_coverage:g}%"
        )
    if unknown_count or invalid_reason_count or runtime_marker_count:
        raise ProductionSurveyAcceptanceError("Survey evidence declarations are incomplete")
    if notification_count != 1 or notification_status != "succeeded":
        raise ProductionSurveyAcceptanceError(
            "Survey completion notification was not delivered exactly once"
        )
    return {
        "survey_id": str(survey_id),
        "job_id": str(job_id),
        "status": "succeeded",
        "manifest_sha256": manifest_sha256,
        "report_sha256": report_sha256,
        "package_sha256": package_sha256,
        "card_count": card_count,
        "section_count": section_count,
        "coverage_percent": coverage_percent,
        "notification_count": notification_count,
        "notification_status": notification_status,
    }


async def _source_survey(source_survey_id: UUID) -> tuple[int, str]:
    row = await get_pool().fetchrow(
        "SELECT user_id, initial_request, status FROM scholight.surveys WHERE id = $1",
        source_survey_id,
    )
    if row is None:
        raise ProductionSurveyOperationError("Source Survey was not found")
    if str(row["status"]) not in _TERMINAL_SURVEY_STATUSES:
        raise ProductionSurveyOperationError("Source Survey is not terminal")
    initial_request = str(row["initial_request"]).strip()
    if not initial_request:
        raise ProductionSurveyOperationError("Source Survey request is unavailable")
    return int(row["user_id"]), initial_request


async def _create_rerun(
    *, source_survey_id: UUID, operation_id: UUID, poll_seconds: float, deadline: float
) -> RerunIdentifiers:
    user_id, initial_request = await _source_survey(source_survey_id)
    identifiers = rerun_identifiers(operation_id)
    survey = await create_survey(
        survey_id=identifiers.survey_id,
        draft_id=identifiers.draft_id,
        user_id=user_id,
        initial_request=initial_request,
        client_request_id=operation_id,
        request_hash=canonical_request_hash(
            operation="create_survey",
            payload={"initial_request": initial_request},
        ),
        quota_date=datetime.now(UTC).date(),
        daily_limit=settings.survey_daily_limit,
    )
    if survey.id != identifiers.survey_id or survey.user_id != user_id:
        raise ProductionSurveyOperationError("Rerun idempotency state is inconsistent")

    while True:
        draft = await get_pool().fetchrow(
            "SELECT status, error_code FROM scholight.survey_drafts "
            "WHERE id = $1 AND survey_id = $2",
            identifiers.draft_id,
            identifiers.survey_id,
        )
        if draft is None:
            raise ProductionSurveyOperationError("Rerun Draft was not found")
        status = str(draft["status"])
        if status == "ready":
            break
        if status in {"failed", "cancelled"}:
            code = str(draft["error_code"] or "survey_draft_failed")
            raise ProductionSurveyOperationError(f"Rerun Draft failed ({code})")
        if time.monotonic() >= deadline:
            raise ProductionSurveyOperationError("Rerun Draft timed out")
        await asyncio.sleep(poll_seconds)

    started = await start_survey(
        survey_id=identifiers.survey_id,
        user_id=user_id,
        job_id=identifiers.job_id,
        client_request_id=identifiers.start_request_id,
        request_hash=canonical_request_hash(
            operation="start_survey",
            payload={
                "survey_id": str(identifiers.survey_id),
                "notify_on_completion": True,
            },
        ),
        notify_on_completion=True,
    )
    if started.id != identifiers.survey_id or started.status not in {
        "queued",
        "running",
        "archiving",
        "succeeded",
    }:
        raise ProductionSurveyOperationError("Rerun could not be started")
    return identifiers


async def _terminal_snapshot(survey_id: UUID, *, poll_seconds: float, deadline: float) -> Any:
    while True:
        row = await get_pool().fetchrow(
            "SELECT s.status AS survey_status, s.error_code AS survey_error_code, "
            "s.quota_state, j.id AS job_id, j.status AS job_status, j.terminal_outcome, "
            "j.error_code AS job_error_code, j.storage_bucket, j.storage_prefix, "
            "j.manifest_key FROM scholight.surveys AS s "
            "LEFT JOIN scholight.survey_jobs AS j ON j.survey_id = s.id "
            "WHERE s.id = $1",
            survey_id,
        )
        if row is None:
            raise ProductionSurveyOperationError("Rerun Survey was not found")
        survey_status = str(row["survey_status"])
        if survey_status in _TERMINAL_SURVEY_STATUSES:
            if survey_status != "succeeded":
                code = str(row["survey_error_code"] or row["job_error_code"] or "survey_failed")
                raise ProductionSurveyAcceptanceError(f"Survey did not succeed ({code})")
            return row
        if time.monotonic() >= deadline:
            raise ProductionSurveyOperationError("Rerun Survey timed out")
        await asyncio.sleep(poll_seconds)


async def _stream_bytes(stream: Any) -> bytes:
    content = bytearray()
    async for chunk in stream.chunks():
        content.extend(chunk)
    return bytes(content)


async def _notification_state(
    survey_id: UUID, *, poll_seconds: float, deadline: float
) -> tuple[int, str | None]:
    while True:
        row = await get_pool().fetchrow(
            "SELECT count(*)::int AS notification_count, min(status) AS notification_status "
            "FROM scholight.survey_email_notifications WHERE survey_id = $1",
            survey_id,
        )
        count = int(row["notification_count"]) if row is not None else 0
        status = str(row["notification_status"]) if row and row["notification_status"] else None
        if count == 1 and status == "succeeded":
            return count, status
        if count > 1 or status == "dead":
            return count, status
        if time.monotonic() >= deadline:
            return count, status
        await asyncio.sleep(poll_seconds)


async def _verify_terminal_survey(
    survey_id: UUID,
    *,
    minimum_coverage: float,
    poll_seconds: float,
    deadline: float,
) -> dict[str, object]:
    from scholight.survey.artifacts import SurveyArtifactStore
    from scholight.survey.evidence import audit_survey_evidence

    snapshot = await _terminal_snapshot(survey_id, poll_seconds=poll_seconds, deadline=deadline)
    job_id = snapshot["job_id"]
    manifest_key = snapshot["manifest_key"]
    bucket = snapshot["storage_bucket"]
    if not isinstance(job_id, UUID) or not isinstance(manifest_key, str) or not manifest_key:
        raise ProductionSurveyAcceptanceError("Survey archive metadata is incomplete")
    if not isinstance(bucket, str) or bucket != settings.survey_s3_bucket:
        raise ProductionSurveyAcceptanceError("Survey archive bucket is invalid")

    store = SurveyArtifactStore(bucket=bucket, endpoint_url=settings.survey_s3_endpoint_url)
    manifest, manifest_sha256 = await store.read_manifest_with_sha256(manifest_key=manifest_key)
    if manifest.get("schema_version") not in {1, 2, 3}:
        raise ProductionSurveyAcceptanceError("Survey manifest version is invalid")

    with tempfile.TemporaryDirectory(
        prefix=".survey-production-verify-", dir=settings.data_root
    ) as directory:
        workspace = Path(directory) / "run"
        await store.restore_contract_workspace(manifest_key=manifest_key, run_root=workspace)
        summary = audit_survey_evidence(workspace)
        card_count = len(tuple((workspace / "cards").glob("*.md")))
        section_count = len(tuple((workspace / "sections").glob("*.md")))
        report_path = workspace / "08_survey.md"
        if not report_path.is_file():
            raise ProductionSurveyAcceptanceError("Survey report is missing")
        report_text = report_path.read_text(encoding="utf-8").casefold()
        if any(marker.casefold() in report_text for marker in _FORBIDDEN_REPORT_MARKERS):
            raise ProductionSurveyAcceptanceError("Survey report exposes runtime metadata")

    report = await store.open_artifact(manifest_key=manifest_key, path="run/08_survey.md")
    report_bytes = await _stream_bytes(report)
    if hashlib.sha256(report_bytes).hexdigest() != report.sha256:
        raise ProductionSurveyAcceptanceError("Survey report checksum is invalid")
    package = await store.build_report_package(manifest_key=manifest_key)
    package_bytes = await _stream_bytes(package)
    package_sha256 = hashlib.sha256(package_bytes).hexdigest()
    notification_count, notification_status = await _notification_state(
        survey_id,
        poll_seconds=poll_seconds,
        deadline=deadline,
    )
    return acceptance_payload(
        survey_id=survey_id,
        job_id=job_id,
        survey_status=str(snapshot["survey_status"]),
        job_status=str(snapshot["job_status"]),
        terminal_outcome=(
            str(snapshot["terminal_outcome"]) if snapshot["terminal_outcome"] is not None else None
        ),
        error_code=(
            str(snapshot["survey_error_code"] or snapshot["job_error_code"])
            if snapshot["survey_error_code"] or snapshot["job_error_code"]
            else None
        ),
        manifest_sha256=manifest_sha256,
        report_sha256=report.sha256,
        package_sha256=package_sha256,
        card_count=card_count,
        section_count=section_count,
        coverage_percent=summary.coverage_percent,
        notification_count=notification_count,
        notification_status=notification_status,
        minimum_coverage=minimum_coverage,
        quota_state=str(snapshot["quota_state"]),
        unknown_count=summary.counts["unknown"],
        invalid_reason_count=summary.invalid_reason_count,
        runtime_marker_count=summary.runtime_marker_count,
    )


async def rerun_and_verify(
    *,
    source_survey_id: UUID,
    operation_id: UUID,
    minimum_coverage: float,
    timeout_seconds: int,
    poll_seconds: float,
) -> dict[str, object]:
    """Create, start, observe, and verify one owner-preserving Survey rerun."""
    deadline = time.monotonic() + timeout_seconds
    await create_pool()
    try:
        identifiers = await _create_rerun(
            source_survey_id=source_survey_id,
            operation_id=operation_id,
            poll_seconds=poll_seconds,
            deadline=deadline,
        )
        payload = await _verify_terminal_survey(
            identifiers.survey_id,
            minimum_coverage=minimum_coverage,
            poll_seconds=poll_seconds,
            deadline=deadline,
        )
        return {"source_survey_id": str(source_survey_id), **payload}
    finally:
        await close_pool()


async def archived_evidence_repair_operation(
    *,
    job_id: UUID,
    apply: bool,
    expected_source_manifest_sha256: str,
    expected_report_sha256: str,
) -> dict[str, object]:
    """Verify or apply one hash-guarded, owner-preserving archived repair."""
    await create_pool()
    try:
        if apply:
            applied = await apply_archived_evidence_repair(
                job_id=job_id,
                expected_source_manifest_sha256=expected_source_manifest_sha256,
                expected_report_sha256=expected_report_sha256,
            )
            result = await inspect_archived_evidence_repair(job_id=job_id)
            if (
                result.manifest_key != applied.manifest_key
                or result.invalid_cards
                or result.coverage_percent < 80.0
                or result.notification_count != applied.notification_count
                or result.notification_status != applied.notification_status
            ):
                raise ProductionSurveyAcceptanceError(
                    "The archived Survey repair did not pass final verification"
                )
            status = "repaired"
            changed = applied.changed
        else:
            result = await inspect_archived_evidence_repair(job_id=job_id)
            status = "eligible"
            changed = False
        if (
            result.source_manifest_sha256 != expected_source_manifest_sha256
            or result.report_sha256 != expected_report_sha256
        ):
            raise ProductionSurveyOperationError("The archived Survey checksum guard changed")
        return {
            "status": status,
            "job_id": str(result.job_id),
            "survey_id": str(result.survey_id),
            "source_manifest_sha256": result.source_manifest_sha256,
            "report_sha256": result.report_sha256,
            "manifest_key": result.manifest_key,
            "manifest_sha256": result.manifest_sha256,
            "invalid_card_count": len(result.invalid_cards),
            "coverage_percent": result.coverage_percent,
            "quota_state": "released",
            "notification_count": result.notification_count,
            "notification_status": result.notification_status,
            "applied": apply,
            "changed": changed,
        }
    except ProductionSurveyOperationError:
        raise
    except Exception as exc:
        raise ProductionSurveyOperationError("Archived Survey evidence repair failed") from exc
    finally:
        await close_pool()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one bounded production Survey operation")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    rerun = subparsers.add_parser("rerun-and-verify")
    rerun.add_argument("--source-survey-id", type=UUID, required=True)
    rerun.add_argument("--operation-id", type=UUID, required=True)
    rerun.add_argument("--minimum-coverage", type=float, default=80.0)
    rerun.add_argument("--timeout-seconds", type=int, default=10_800)
    rerun.add_argument("--poll-seconds", type=float, default=30.0)
    rerun.add_argument("--notify-on-completion", action="store_true", required=True)
    repair = subparsers.add_parser("repair-degraded-evidence")
    repair.add_argument("--job-id", type=UUID, required=True)
    repair.add_argument("--expected-source-manifest-sha256", required=True)
    repair.add_argument("--expected-report-sha256", required=True)
    repair.add_argument("--apply", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        if args.operation == "rerun-and-verify":
            if not 0.0 <= args.minimum_coverage <= 100.0:
                raise SystemExit("minimum coverage must be between 0 and 100")
            if not 60 <= args.timeout_seconds <= 14_400:
                raise SystemExit("timeout must be between 60 and 14400 seconds")
            if not 1.0 <= args.poll_seconds <= 300.0:
                raise SystemExit("poll interval must be between 1 and 300 seconds")
            payload = asyncio.run(
                rerun_and_verify(
                    source_survey_id=args.source_survey_id,
                    operation_id=args.operation_id,
                    minimum_coverage=args.minimum_coverage,
                    timeout_seconds=args.timeout_seconds,
                    poll_seconds=args.poll_seconds,
                )
            )
        else:
            payload = asyncio.run(
                archived_evidence_repair_operation(
                    job_id=args.job_id,
                    apply=args.apply,
                    expected_source_manifest_sha256=args.expected_source_manifest_sha256,
                    expected_report_sha256=args.expected_report_sha256,
                )
            )
    except ProductionSurveyOperationError as exc:
        sys.stdout.write(
            json.dumps({"status": "failed", "error": str(exc)}, separators=(",", ":")) + "\n"
        )
        raise SystemExit(1) from exc
    sys.stdout.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
