"""Verified, append-only recovery of archived Survey finalization failures."""

from __future__ import annotations

import hashlib
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import structlog

from scholight.config import settings
from scholight.db.client import get_pool
from scholight.db.queries_survey import recover_archived_survey_contract_failure
from scholight.survey.artifacts import SurveyArtifactStore, SurveyRecoveryOverlay
from scholight.survey.diagnostics import SurveyDiagnostics
from scholight.survey.finalizer import finalize_survey

logger = structlog.get_logger(__name__)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPORT_MISSING_CODES = frozenset(
    {
        "survey_report_missing",
        "survey_outline_metadata_invalid",
        "survey_section_contract_invalid",
        "survey_reference_contract_invalid",
        "survey_finalization_output_invalid",
    }
)
_MAX_LEGACY_RECOVERY_CARD_PLAN_ITEMS = 256
RecoveryType = Literal["exact_report_reclassification", "deterministic_finalization"]


class ArchivedSurveyRecoveryError(RuntimeError):
    """An archived Survey could not be proven safe to recover."""


@dataclass(frozen=True, slots=True)
class ArchivedSurveyRecovery:
    job_id: UUID
    survey_id: UUID
    user_id: int
    source_manifest_key: str
    source_manifest_sha256: str
    manifest_key: str
    recovery_type: RecoveryType
    expected_manifest: dict[str, Any]
    report_sha256: str
    index_sha256: str
    verified_file_count: int
    contract_warning_count: int
    applied: bool
    changed: bool

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["job_id"] = str(self.job_id)
        payload["survey_id"] = str(self.survey_id)
        return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_hash(manifest: dict[str, Any], path: str) -> str:
    matches = [
        record
        for record in manifest.get("files", [])
        if isinstance(record, dict) and record.get("path") == path
    ]
    if (
        len(matches) != 1
        or not isinstance(matches[0].get("sha256"), str)
        or _SHA256.fullmatch(matches[0]["sha256"]) is None
    ):
        raise ArchivedSurveyRecoveryError("The archived final artifact is not uniquely identified")
    return str(matches[0]["sha256"])


def _validate_guard(value: str | None, *, label: str) -> None:
    if value is not None and _SHA256.fullmatch(value) is None:
        raise ArchivedSurveyRecoveryError(f"Expected {label} must be 64 lowercase hex digits")


def _validate_owner(
    manifest: dict[str, Any],
    *,
    user_id: int,
    job_id: UUID,
) -> None:
    if manifest.get("user_id") != user_id or manifest.get("job_id") != str(job_id):
        raise ArchivedSurveyRecoveryError("The archived Survey manifest ownership is invalid")


async def _already_recovered_result(
    *,
    job_id: UUID,
    survey_id: UUID,
    user_id: int,
    manifest_key: str,
    manifest: dict[str, Any],
    manifest_sha256: str,
    expected_source_manifest_sha256: str | None,
    expected_report_sha256: str | None,
    apply: bool,
) -> ArchivedSurveyRecovery:
    if manifest.get("schema_version") == 2:
        parent = manifest.get("parent_manifest")
        if not isinstance(parent, dict):
            raise ArchivedSurveyRecoveryError("The recovered Survey parent manifest is invalid")
        source_manifest_key = parent.get("key")
        source_manifest_sha256 = parent.get("sha256")
        recovery_type: RecoveryType = "deterministic_finalization"
        if not isinstance(source_manifest_key, str) or not isinstance(source_manifest_sha256, str):
            raise ArchivedSurveyRecoveryError("The recovered Survey parent manifest is invalid")
    elif manifest.get("schema_version") == 1:
        source_manifest_key = manifest_key
        source_manifest_sha256 = manifest_sha256
        recovery_type = "exact_report_reclassification"
    else:
        raise ArchivedSurveyRecoveryError("The recovered Survey manifest version is invalid")
    report_sha256 = _manifest_hash(manifest, "run/08_survey.md")
    index_sha256 = _manifest_hash(manifest, "run/index.md")
    if (
        expected_source_manifest_sha256 is not None
        and expected_source_manifest_sha256 != source_manifest_sha256
    ):
        raise ArchivedSurveyRecoveryError("The source manifest SHA256 does not match the guard")
    if expected_report_sha256 is not None and expected_report_sha256 != report_sha256:
        raise ArchivedSurveyRecoveryError("The archived report SHA256 does not match the guard")
    return ArchivedSurveyRecovery(
        job_id=job_id,
        survey_id=survey_id,
        user_id=user_id,
        source_manifest_key=source_manifest_key,
        source_manifest_sha256=source_manifest_sha256,
        manifest_key=manifest_key,
        recovery_type=recovery_type,
        expected_manifest=manifest,
        report_sha256=report_sha256,
        index_sha256=index_sha256,
        verified_file_count=len(manifest.get("files", [])),
        contract_warning_count=0,
        applied=apply,
        changed=False,
    )


async def recover_archived_survey(
    *,
    job_id: UUID,
    apply: bool = False,
    expected_source_manifest_sha256: str | None = None,
    expected_report_sha256: str | None = None,
    artifact_store: SurveyArtifactStore | None = None,
) -> ArchivedSurveyRecovery:
    """Dry-run or apply one hash-guarded archived Survey recovery."""
    _validate_guard(expected_source_manifest_sha256, label="source manifest SHA256")
    _validate_guard(expected_report_sha256, label="report SHA256")
    if apply and (expected_source_manifest_sha256 is None or expected_report_sha256 is None):
        raise ArchivedSurveyRecoveryError(
            "Applying recovery requires the expected source manifest and report SHA256"
        )

    row = await get_pool().fetchrow(
        "SELECT j.id AS job_id, j.survey_id, s.user_id, "
        "j.status AS job_status, j.terminal_outcome, j.error_code AS job_error_code, "
        "j.storage_bucket, j.storage_prefix, j.manifest_key, "
        "s.status AS survey_status, s.quota_state, s.error_code AS survey_error_code "
        "FROM scholight.survey_jobs AS j "
        "JOIN scholight.surveys AS s ON s.id = j.survey_id WHERE j.id = $1",
        job_id,
    )
    if row is None:
        raise ArchivedSurveyRecoveryError("The archived Survey job was not found")
    failed_error_code = str(row["job_error_code"] or "")
    is_failed = (
        row["job_status"] == "finished"
        and row["terminal_outcome"] == "failed"
        and row["survey_status"] == "failed"
        and row["quota_state"] == "released"
        and row["survey_error_code"] == row["job_error_code"]
        and (
            failed_error_code == "survey_contract_violation"
            or failed_error_code in _REPORT_MISSING_CODES
        )
    )
    is_already_recovered = (
        row["job_status"] == "finished"
        and row["terminal_outcome"] == "succeeded"
        and row["survey_status"] == "succeeded"
        and row["quota_state"] == "consumed"
    )
    if not (is_failed or is_already_recovered):
        raise ArchivedSurveyRecoveryError("The archived Survey is not an eligible failure")

    user_id = int(row["user_id"])
    survey_id = row["survey_id"]
    expected_prefix = SurveyArtifactStore.prefix(user_id=user_id, job_id=job_id)
    manifest_key = row["manifest_key"]
    if row["storage_prefix"] != expected_prefix or not isinstance(manifest_key, str):
        raise ArchivedSurveyRecoveryError("The archived Survey ownership prefix is invalid")
    is_base_manifest = manifest_key == f"{expected_prefix}/manifest.json"
    is_overlay_manifest = manifest_key.startswith(
        f"{expected_prefix}/recoveries/"
    ) and manifest_key.endswith("/manifest.json")
    if not (is_base_manifest or (is_already_recovered and is_overlay_manifest)):
        raise ArchivedSurveyRecoveryError("The archived Survey ownership prefix is invalid")

    bucket = str(row["storage_bucket"] or settings.survey_s3_bucket)
    store = artifact_store or SurveyArtifactStore(
        bucket=bucket,
        endpoint_url=settings.survey_s3_endpoint_url,
    )
    manifest, manifest_sha256 = await store.read_manifest_with_sha256(manifest_key=manifest_key)
    await store.validate_manifest(manifest_key=manifest_key)
    _validate_owner(manifest, user_id=user_id, job_id=job_id)
    if is_already_recovered:
        return await _already_recovered_result(
            job_id=job_id,
            survey_id=survey_id,
            user_id=user_id,
            manifest_key=manifest_key,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            expected_source_manifest_sha256=expected_source_manifest_sha256,
            expected_report_sha256=expected_report_sha256,
            apply=apply,
        )
    if manifest.get("schema_version") != 1:
        raise ArchivedSurveyRecoveryError("The failed Survey source manifest must be v1")
    if (
        expected_source_manifest_sha256 is not None
        and expected_source_manifest_sha256 != manifest_sha256
    ):
        raise ArchivedSurveyRecoveryError("The source manifest SHA256 does not match the guard")

    recovery_type: RecoveryType = (
        "exact_report_reclassification"
        if failed_error_code == "survey_contract_violation"
        else "deterministic_finalization"
    )
    archived_report_sha: str | None = None
    archived_index_sha: str | None = None
    if recovery_type == "exact_report_reclassification":
        archived_report_sha = _manifest_hash(manifest, "run/08_survey.md")
        archived_index_sha = _manifest_hash(manifest, "run/index.md")

    overlay: SurveyRecoveryOverlay | None = None
    with tempfile.TemporaryDirectory(prefix="scholight-survey-recovery-") as directory:
        run_root = Path(directory) / "run"
        restored = await store.restore_contract_workspace(
            manifest_key=manifest_key,
            run_root=run_root,
        )
        diagnostics = SurveyDiagnostics(
            run_root=run_root,
            job_id=job_id,
            survey_id=survey_id,
        )
        if recovery_type == "deterministic_finalization":
            for plan, max_items in (
                ("00_card_plan.json", _MAX_LEGACY_RECOVERY_CARD_PLAN_ITEMS),
                ("00_sections.json", None),
            ):
                missing = diagnostics.missing_durable_plan_items(
                    plan,
                    accept_archived_run_dir=True,
                    max_items=max_items,
                )
                if missing is None or missing:
                    raise ArchivedSurveyRecoveryError(
                        "The archived Survey does not have complete validated plans"
                    )
        finalized = finalize_survey(run_root)
        diagnostics.finalize_recovery_audit()
        anomalies = diagnostics.snapshot()["anomalies"]
        errors = [item for item in anomalies if item.get("severity") == "error"]
        warnings = [item for item in anomalies if item.get("severity") == "warning"]
        if errors:
            first = errors[0]
            raise ArchivedSurveyRecoveryError(
                "The restored Survey still violates its artifact contract: "
                f"{first.get('kind', 'unknown')}"
            )
        report_sha = _sha256(finalized.report_path)
        index_sha = _sha256(finalized.index_path)
        if expected_report_sha256 is not None and expected_report_sha256 != report_sha:
            raise ArchivedSurveyRecoveryError("The archived report SHA256 does not match the guard")
        if recovery_type == "exact_report_reclassification":
            if report_sha != archived_report_sha or index_sha != archived_index_sha:
                raise ArchivedSurveyRecoveryError(
                    "Deterministic finalization does not match the immutable archive"
                )
            expected_manifest = manifest
            target_manifest_key = manifest_key
        else:
            overlay = await store.plan_recovery_overlay(
                source_manifest_key=manifest_key,
                expected_source_sha256=manifest_sha256,
                report_path=finalized.report_path,
                index_path=finalized.index_path,
            )
            expected_manifest = overlay.manifest
            target_manifest_key = overlay.manifest_key
            if apply:
                written = await store.create_recovery_overlay(
                    source_manifest_key=manifest_key,
                    expected_source_sha256=manifest_sha256,
                    report_path=finalized.report_path,
                    index_path=finalized.index_path,
                )
                if (
                    written.manifest_key != target_manifest_key
                    or written.manifest != expected_manifest
                ):
                    raise ArchivedSurveyRecoveryError("The recovery overlay changed while writing")

    changed = False
    if apply:
        changed = await recover_archived_survey_contract_failure(
            job_id=job_id,
            expected_manifest_key=manifest_key,
            expected_error_code=failed_error_code,
            replacement_manifest_key=(
                overlay.manifest_key
                if recovery_type == "deterministic_finalization" and overlay
                else None
            ),
        )
    logger.info(
        "survey_archived_recovery_verified",
        job_id=str(job_id),
        survey_id=str(survey_id),
        source_manifest_key=manifest_key,
        source_manifest_sha256=manifest_sha256,
        manifest_key=target_manifest_key,
        recovery_type=recovery_type,
        report_sha256=report_sha,
        verified_file_count=len(restored),
        applied=apply,
        changed=changed,
    )
    return ArchivedSurveyRecovery(
        job_id=job_id,
        survey_id=survey_id,
        user_id=user_id,
        source_manifest_key=manifest_key,
        source_manifest_sha256=manifest_sha256,
        manifest_key=target_manifest_key,
        recovery_type=recovery_type,
        expected_manifest=expected_manifest,
        report_sha256=report_sha,
        index_sha256=index_sha,
        verified_file_count=len(restored),
        contract_warning_count=len(warnings),
        applied=apply,
        changed=changed,
    )


__all__ = [
    "ArchivedSurveyRecovery",
    "ArchivedSurveyRecoveryError",
    "recover_archived_survey",
]
