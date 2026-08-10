"""Verified, in-place recovery of archived Survey contract failures."""

from __future__ import annotations

import hashlib
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog

from scholight.config import settings
from scholight.db.client import get_pool
from scholight.db.queries_survey import recover_archived_survey_contract_failure
from scholight.survey.artifacts import SurveyArtifactStore
from scholight.survey.diagnostics import SurveyDiagnostics
from scholight.survey.finalizer import finalize_survey

logger = structlog.get_logger(__name__)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArchivedSurveyRecoveryError(RuntimeError):
    """An archived Survey could not be proven safe to recover."""


@dataclass(frozen=True, slots=True)
class ArchivedSurveyRecovery:
    job_id: UUID
    survey_id: UUID
    user_id: int
    manifest_key: str
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


async def recover_archived_survey(
    *,
    job_id: UUID,
    apply: bool = False,
    expected_report_sha256: str | None = None,
    artifact_store: SurveyArtifactStore | None = None,
) -> ArchivedSurveyRecovery:
    """Verify an archive and optionally apply the guarded database transition."""
    if expected_report_sha256 is not None and _SHA256.fullmatch(expected_report_sha256) is None:
        raise ArchivedSurveyRecoveryError("Expected report SHA256 must be 64 lowercase hex digits")
    if apply and expected_report_sha256 is None:
        raise ArchivedSurveyRecoveryError("Applying recovery requires the expected report SHA256")

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
    is_failed_contract = (
        row["job_status"] == "finished"
        and row["terminal_outcome"] == "failed"
        and row["job_error_code"] == "survey_contract_violation"
        and row["survey_status"] == "failed"
        and row["quota_state"] == "released"
        and row["survey_error_code"] == "survey_contract_violation"
    )
    is_already_recovered = (
        row["job_status"] == "finished"
        and row["terminal_outcome"] == "succeeded"
        and row["survey_status"] == "succeeded"
        and row["quota_state"] == "consumed"
    )
    if not (is_failed_contract or is_already_recovered):
        raise ArchivedSurveyRecoveryError("The archived Survey is not an eligible contract failure")

    user_id = int(row["user_id"])
    survey_id = row["survey_id"]
    expected_prefix = SurveyArtifactStore.prefix(user_id=user_id, job_id=job_id)
    manifest_key = row["manifest_key"]
    if (
        row["storage_prefix"] != expected_prefix
        or manifest_key != f"{expected_prefix}/manifest.json"
    ):
        raise ArchivedSurveyRecoveryError("The archived Survey ownership prefix is invalid")
    bucket = str(row["storage_bucket"] or settings.survey_s3_bucket)
    store = artifact_store or SurveyArtifactStore(
        bucket=bucket,
        endpoint_url=settings.survey_s3_endpoint_url,
    )
    manifest = await store.read_manifest(manifest_key=manifest_key)
    archived_report_sha = _manifest_hash(manifest, "run/08_survey.md")
    archived_index_sha = _manifest_hash(manifest, "run/index.md")
    if expected_report_sha256 is not None and expected_report_sha256 != archived_report_sha:
        raise ArchivedSurveyRecoveryError("The archived report SHA256 does not match the guard")

    with tempfile.TemporaryDirectory(prefix="scholight-survey-recovery-") as directory:
        run_root = Path(directory) / "run"
        restored = await store.restore_contract_workspace(
            manifest_key=manifest_key,
            run_root=run_root,
        )
        if "08_survey.md" not in restored or "index.md" not in restored:
            raise ArchivedSurveyRecoveryError("The archived final artifacts are incomplete")
        finalized = finalize_survey(run_root)
        diagnostics = SurveyDiagnostics(
            run_root=run_root,
            job_id=job_id,
            survey_id=survey_id,
        )
        diagnostics.finalize_contract_audit()
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
        if report_sha != archived_report_sha or index_sha != archived_index_sha:
            raise ArchivedSurveyRecoveryError(
                "Deterministic finalization does not match the immutable archive"
            )

    changed = False
    if apply:
        changed = await recover_archived_survey_contract_failure(
            job_id=job_id,
            expected_manifest_key=manifest_key,
        )
    logger.info(
        "survey_archived_recovery_verified",
        job_id=str(job_id),
        survey_id=str(survey_id),
        manifest_key=manifest_key,
        report_sha256=report_sha,
        verified_file_count=len(restored),
        applied=apply,
        changed=changed,
    )
    return ArchivedSurveyRecovery(
        job_id=job_id,
        survey_id=survey_id,
        user_id=user_id,
        manifest_key=manifest_key,
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
