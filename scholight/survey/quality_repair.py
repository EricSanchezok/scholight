"""Hash-guarded, append-only repair of archived degraded Survey evidence."""

from __future__ import annotations

import hashlib
import re
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog

from scholight.config import settings
from scholight.db.client import get_pool
from scholight.db.queries_survey import repair_degraded_survey_evidence
from scholight.survey.artifacts import SurveyArtifactStore
from scholight.survey.diagnostics import SurveyDiagnostics
from scholight.survey.evidence import audit_survey_evidence, summarize_survey_evidence
from scholight.survey.finalizer import finalize_survey
from scholight.survey.process import ProcessControl
from scholight.survey.worker import (
    SurveyRepairContext,
    _invalid_evidence_repair_items,
    _run_repair_workflow,
)

logger = structlog.get_logger(__name__)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArchivedEvidenceRepairError(RuntimeError):
    """An archived degraded Survey could not be safely repaired."""


@dataclass(frozen=True, slots=True)
class ArchivedEvidenceRepair:
    job_id: UUID
    survey_id: UUID
    user_id: int
    source_manifest_key: str
    source_manifest_sha256: str
    report_sha256: str
    manifest_key: str
    manifest_sha256: str
    invalid_cards: tuple[str, ...]
    coverage_percent: float
    notification_count: int
    notification_status: str | None
    applied: bool
    changed: bool

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["job_id"] = str(self.job_id)
        payload["survey_id"] = str(self.survey_id)
        return payload


async def _repair_row(job_id: UUID) -> Any:
    row = await get_pool().fetchrow(
        "SELECT j.id AS job_id, j.survey_id, s.user_id, "
        "s.status AS survey_status, s.quota_state, s.error_code AS survey_error_code, "
        "j.status AS job_status, j.terminal_outcome, j.error_code AS job_error_code, "
        "j.storage_bucket, j.storage_prefix, j.manifest_key, "
        "(SELECT count(*)::int FROM scholight.survey_email_notifications AS n "
        "WHERE n.survey_id = j.survey_id) AS notification_count, "
        "(SELECT min(n.status) FROM scholight.survey_email_notifications AS n "
        "WHERE n.survey_id = j.survey_id) AS notification_status "
        "FROM scholight.survey_jobs AS j "
        "JOIN scholight.surveys AS s ON s.id = j.survey_id WHERE j.id = $1",
        job_id,
    )
    if row is None:
        raise ArchivedEvidenceRepairError("The archived Survey job was not found")
    degraded = (
        row["survey_status"] == "succeeded"
        and row["quota_state"] == "released"
        and row["survey_error_code"] == "survey_quality_degraded"
        and row["job_status"] == "finished"
        and row["terminal_outcome"] == "succeeded"
        and row["job_error_code"] == "survey_quality_degraded"
    )
    repaired = (
        row["survey_status"] == "succeeded"
        and row["quota_state"] == "released"
        and row["survey_error_code"] is None
        and row["job_status"] == "finished"
        and row["terminal_outcome"] == "succeeded"
        and row["job_error_code"] is None
    )
    if not (degraded or repaired):
        raise ArchivedEvidenceRepairError("The archived Survey is not eligible for evidence repair")
    if int(row["notification_count"]) > 1:
        raise ArchivedEvidenceRepairError("The archived Survey has duplicate notifications")
    return row


def _manifest_report_sha256(manifest: dict[str, Any]) -> str:
    matches = [
        record
        for record in manifest.get("files", [])
        if isinstance(record, dict) and record.get("path") == "run/08_survey.md"
    ]
    if (
        len(matches) != 1
        or not isinstance(matches[0].get("sha256"), str)
        or _SHA256.fullmatch(matches[0]["sha256"]) is None
    ):
        raise ArchivedEvidenceRepairError("The archived report is not uniquely identified")
    return str(matches[0]["sha256"])


def _validate_manifest_owner(
    manifest: dict[str, Any],
    *,
    user_id: int,
    job_id: UUID,
) -> None:
    if manifest.get("user_id") != user_id or manifest.get("job_id") != str(job_id):
        raise ArchivedEvidenceRepairError("The archived Survey manifest ownership is invalid")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


async def _source_manifest(
    *,
    row: Any,
    store: SurveyArtifactStore,
) -> tuple[str, dict[str, Any], str, str, str, bool]:
    job_id = row["job_id"]
    user_id = int(row["user_id"])
    prefix = SurveyArtifactStore.prefix(user_id=user_id, job_id=job_id)
    active_key = row["manifest_key"]
    if row["storage_prefix"] != prefix or not isinstance(active_key, str):
        raise ArchivedEvidenceRepairError("The archived Survey ownership prefix is invalid")
    active, active_sha256 = await store.read_manifest_with_sha256(manifest_key=active_key)
    _validate_manifest_owner(active, user_id=user_id, job_id=job_id)
    already_repaired = row["survey_error_code"] is None
    if already_repaired:
        if (
            active.get("schema_version") != 3
            or active.get("repair_type") != "evidence_declarations"
        ):
            raise ArchivedEvidenceRepairError("The repaired Survey manifest is invalid")
        parent = active.get("parent_manifest")
        source_key = parent.get("key") if isinstance(parent, dict) else None
        expected_source_sha256 = parent.get("sha256") if isinstance(parent, dict) else None
        if (
            source_key != f"{prefix}/manifest.json"
            or not isinstance(expected_source_sha256, str)
            or _SHA256.fullmatch(expected_source_sha256) is None
        ):
            raise ArchivedEvidenceRepairError("The repaired Survey parent manifest is invalid")
        source, source_sha256 = await store.read_manifest_with_sha256(manifest_key=source_key)
        if source_sha256 != expected_source_sha256:
            raise ArchivedEvidenceRepairError("The repaired Survey parent checksum is invalid")
    else:
        source_key = active_key
        source = active
        source_sha256 = active_sha256
        if source_key != f"{prefix}/manifest.json":
            raise ArchivedEvidenceRepairError("The archived Survey source manifest is invalid")
    if source.get("schema_version") != 1:
        raise ArchivedEvidenceRepairError("The archived Survey source manifest must be v1")
    _validate_manifest_owner(source, user_id=user_id, job_id=job_id)
    return source_key, source, source_sha256, active_key, active_sha256, already_repaired


async def inspect_archived_evidence_repair(
    *,
    job_id: UUID,
    artifact_store: SurveyArtifactStore | None = None,
) -> ArchivedEvidenceRepair:
    """Inspect eligibility and emit immutable guards without invoking a model or writing data."""
    row = await _repair_row(job_id)
    bucket = str(row["storage_bucket"] or settings.survey_s3_bucket)
    store = artifact_store or SurveyArtifactStore(
        bucket=bucket,
        endpoint_url=settings.survey_s3_endpoint_url,
    )
    (
        source_key,
        source,
        source_sha256,
        active_key,
        active_sha256,
        already_repaired,
    ) = await _source_manifest(row=row, store=store)
    await store.validate_manifest(manifest_key=active_key)
    with tempfile.TemporaryDirectory(
        prefix=".survey-evidence-repair-",
        dir=settings.data_root,
    ) as directory:
        run_root = Path(directory) / "run"
        await store.restore_contract_workspace(manifest_key=active_key, run_root=run_root)
        summary = summarize_survey_evidence(run_root)
        invalid_cards = summary.invalid_cards
        if already_repaired:
            if invalid_cards or summary.runtime_marker_count or summary.coverage_percent < 80.0:
                raise ArchivedEvidenceRepairError("The repaired Survey evidence is incomplete")
        else:
            diagnostics = SurveyDiagnostics(
                run_root=run_root,
                job_id=job_id,
                survey_id=row["survey_id"],
            )
            selected = _invalid_evidence_repair_items(diagnostics, invalid_cards)
            if not selected:
                raise ArchivedEvidenceRepairError(
                    "The archived Survey invalid cards do not match its durable plan"
                )
    return ArchivedEvidenceRepair(
        job_id=job_id,
        survey_id=row["survey_id"],
        user_id=int(row["user_id"]),
        source_manifest_key=source_key,
        source_manifest_sha256=source_sha256,
        report_sha256=_manifest_report_sha256(source),
        manifest_key=active_key,
        manifest_sha256=active_sha256,
        invalid_cards=invalid_cards,
        coverage_percent=summary.coverage_percent,
        notification_count=int(row["notification_count"]),
        notification_status=(
            str(row["notification_status"]) if row["notification_status"] is not None else None
        ),
        applied=False,
        changed=False,
    )


async def apply_archived_evidence_repair(
    *,
    job_id: UUID,
    expected_source_manifest_sha256: str,
    expected_report_sha256: str,
    artifact_store: SurveyArtifactStore | None = None,
) -> ArchivedEvidenceRepair:
    """Repair selected cards and atomically activate a verified append-only overlay."""
    if (
        _SHA256.fullmatch(expected_source_manifest_sha256) is None
        or _SHA256.fullmatch(expected_report_sha256) is None
    ):
        raise ArchivedEvidenceRepairError("Evidence repair checksum guards are invalid")
    inspection = await inspect_archived_evidence_repair(
        job_id=job_id,
        artifact_store=artifact_store,
    )
    if (
        inspection.source_manifest_sha256 != expected_source_manifest_sha256
        or inspection.report_sha256 != expected_report_sha256
    ):
        raise ArchivedEvidenceRepairError("The archived Survey checksum guard changed")
    if not inspection.invalid_cards:
        return replace(inspection, applied=True)

    row = await _repair_row(job_id)
    bucket = str(row["storage_bucket"] or settings.survey_s3_bucket)
    store = artifact_store or SurveyArtifactStore(
        bucket=bucket,
        endpoint_url=settings.survey_s3_endpoint_url,
    )
    with tempfile.TemporaryDirectory(
        prefix=".survey-evidence-repair-",
        dir=settings.data_root,
    ) as directory:
        run_root = Path(directory) / "run"
        restored = await store.restore_contract_workspace(
            manifest_key=inspection.source_manifest_key,
            run_root=run_root,
        )
        diagnostics = SurveyDiagnostics(
            run_root=run_root,
            job_id=job_id,
            survey_id=inspection.survey_id,
        )
        selected = _invalid_evidence_repair_items(diagnostics, inspection.invalid_cards)
        if not selected:
            raise ArchivedEvidenceRepairError(
                "The archived Survey invalid cards do not match its durable plan"
            )
        context = SurveyRepairContext(
            id=job_id,
            survey_id=inspection.survey_id,
            user_id=inspection.user_id,
        )
        repaired = await _run_repair_workflow(
            job=context,
            run_root=run_root,
            plan="00_card_plan.json",
            workflow="card_repair.rcm",
            control=ProcessControl(),
            invalid_evidence_items=selected,
        )
        if not repaired:
            raise ArchivedEvidenceRepairError("The archived Survey card repair did not complete")
        summary = audit_survey_evidence(run_root)
        if (
            summary.counts["unknown"]
            or summary.invalid_reason_count
            or summary.runtime_marker_count
            or summary.coverage_percent < 80.0
        ):
            raise ArchivedEvidenceRepairError("The repaired Survey evidence is still incomplete")
        finalized = finalize_survey(run_root)
        allowed_changes = {
            *inspection.invalid_cards,
            "08_survey.md",
            "index.md",
        }
        for relative_path, expected_sha256 in restored.items():
            if relative_path not in allowed_changes:
                path = run_root.joinpath(*Path(relative_path).parts)
                if _sha256(path) != expected_sha256:
                    raise ArchivedEvidenceRepairError(
                        "The evidence repair changed an unrelated archived artifact"
                    )
        overlay = await store.create_evidence_repair_overlay(
            source_manifest_key=inspection.source_manifest_key,
            expected_source_sha256=inspection.source_manifest_sha256,
            run_root=run_root,
            repaired_cards=inspection.invalid_cards,
        )
        await store.validate_manifest(manifest_key=overlay.manifest_key)
        _, overlay_manifest_sha256 = await store.read_manifest_with_sha256(
            manifest_key=overlay.manifest_key
        )
        if finalized.report_path != run_root / "08_survey.md":
            raise ArchivedEvidenceRepairError("The repaired Survey report path is invalid")

    changed = await repair_degraded_survey_evidence(
        job_id=job_id,
        expected_manifest_key=inspection.source_manifest_key,
        replacement_manifest_key=overlay.manifest_key,
    )
    logger.info(
        "survey_archived_evidence_repair_verified",
        job_id=str(job_id),
        survey_id=str(inspection.survey_id),
        source_manifest_sha256=inspection.source_manifest_sha256,
        manifest_key=overlay.manifest_key,
        invalid_card_count=len(inspection.invalid_cards),
        applied=True,
        changed=changed,
    )
    return replace(
        inspection,
        manifest_key=overlay.manifest_key,
        manifest_sha256=overlay_manifest_sha256,
        coverage_percent=summary.coverage_percent,
        applied=True,
        changed=changed,
    )


__all__ = [
    "ArchivedEvidenceRepair",
    "ArchivedEvidenceRepairError",
    "apply_archived_evidence_repair",
    "inspect_archived_evidence_repair",
]
