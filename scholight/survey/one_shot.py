"""Exact one-work-item entry points for standalone Survey compute tasks."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import UUID

from scholight.config import settings
from scholight.db.client import DBError
from scholight.db.queries_survey import SurveyJob
from scholight.db.queries_survey_attempts import (
    claim_exact_survey_draft,
    claim_exact_survey_job,
    commit_survey_job_checkpoint,
    get_claimed_job_checkpoint,
    record_compute_attempt_diagnostics,
)
from scholight.survey.artifacts import SurveyArtifactStore
from scholight.survey.checkpoints import SurveyCheckpointStore
from scholight.survey.draft_worker import process_survey_draft
from scholight.survey.process import ProcessControl
from scholight.survey.rcm_diagnostics import attempt_failure_details
from scholight.survey.resumable_runner import execute_resumable_survey
from scholight.survey.worker import (
    RCM_VERSION,
    WORKFLOW_VERSION,
    SurveyExecutionResult,
    process_survey_job,
)

EXECUTOR_VERSION = "survey-dag-v1"


async def _record_result_provider_diagnostics(
    *, attempt_id: UUID, result: SurveyExecutionResult
) -> None:
    diagnostics = result.diagnostics
    if not isinstance(diagnostics, dict):
        return
    raw_failures = diagnostics.get("provider_failures")
    if not isinstance(raw_failures, list):
        return
    failure = next((item for item in reversed(raw_failures) if isinstance(item, dict)), None)
    if failure is None:
        return
    details = attempt_failure_details(failure)
    if not details:
        return
    request_class = details.get("provider_request_class")
    failure_class = f"provider_{request_class}" if isinstance(request_class, str) else "provider"
    try:
        await record_compute_attempt_diagnostics(
            attempt_id=attempt_id,
            failure_class=failure_class,
            failure_details=details,
        )
    except DBError:
        # The query layer emits a content-free operational error. Diagnostics
        # are best effort and must not replace the Survey's actual outcome.
        return


def job_run_root(job_id: UUID) -> Path:
    return Path(settings.data_root) / "surveys" / str(job_id) / "run"


async def run_exact_draft(*, draft_id: UUID, attempt_id: UUID) -> bool:
    """Run the one Draft reserved for ``attempt_id`` and then return."""
    draft = await claim_exact_survey_draft(
        draft_id=draft_id,
        attempt_id=attempt_id,
        lease_seconds=settings.survey_lease_seconds,
    )
    if draft is None:
        return False
    await process_survey_draft(
        draft=draft,
        worker_id=attempt_id,
        attempt_id=attempt_id,
    )
    return True


async def run_exact_job(*, job_id: UUID, attempt_id: UUID) -> bool:
    """Restore and run one exact Full Survey attempt, then let the task exit."""
    claimed_job = await claim_exact_survey_job(
        job_id=job_id,
        attempt_id=attempt_id,
        lease_seconds=settings.survey_lease_seconds,
        workflow_version=WORKFLOW_VERSION,
        executor_version=EXECUTOR_VERSION,
        execution_timeout_seconds=settings.survey_job_timeout_seconds,
    )
    if claimed_job is None:
        return False
    pointer = await get_claimed_job_checkpoint(job_id=job_id, attempt_id=attempt_id)
    run_root = job_run_root(job_id)
    checkpoint_store = SurveyCheckpointStore(
        bucket=settings.survey_s3_bucket,
        endpoint_url=settings.survey_s3_endpoint_url,
    )
    successor = await checkpoint_store.find_successor(
        user_id=claimed_job.user_id,
        job_id=claimed_job.id,
        expected_sequence=pointer.sequence + 1,
        parent_manifest_sha256=pointer.manifest_sha256,
        workflow_version=pointer.workflow_version,
        executor_version=pointer.executor_version,
    )
    if successor is not None:
        adopted = await commit_survey_job_checkpoint(
            job_id=claimed_job.id,
            attempt_id=attempt_id,
            expected_sequence=pointer.sequence,
            stage=successor.stage,
            manifest_key=successor.manifest_key,
            manifest_sha256=successor.manifest_sha256,
        )
        if not adopted:
            raise RuntimeError("Survey checkpoint successor could not be fenced")
        pointer = replace(
            pointer,
            sequence=successor.sequence,
            stage=successor.stage,
            manifest_key=successor.manifest_key,
            manifest_sha256=successor.manifest_sha256,
        )
    restored = None
    if pointer.manifest_key is not None and pointer.manifest_sha256 is not None:
        restored = await checkpoint_store.restore(
            user_id=claimed_job.user_id,
            job_id=claimed_job.id,
            run_root=run_root,
            manifest_key=pointer.manifest_key,
            manifest_sha256=pointer.manifest_sha256,
        )
    else:
        run_root.mkdir(parents=True, exist_ok=True)
    artifact_store = SurveyArtifactStore(
        bucket=settings.survey_s3_bucket,
        endpoint_url=settings.survey_s3_endpoint_url,
    )

    async def _execute(
        job: SurveyJob,
        run_root: Path,
        *,
        control: ProcessControl,
    ) -> SurveyExecutionResult:
        if job is not claimed_job:
            raise RuntimeError("One-shot Survey executor received a different job")
        result = await execute_resumable_survey(
            claimed_job,
            run_root,
            control=control,
            checkpoint_store=checkpoint_store,
            checkpoint_pointer=pointer,
            restored_checkpoint=restored,
            attempt_id=attempt_id,
        )
        await _record_result_provider_diagnostics(attempt_id=attempt_id, result=result)
        return result

    await process_survey_job(
        job=claimed_job,
        worker_id=attempt_id,
        artifact_store=artifact_store,
        attempt_id=attempt_id,
        execute_job=_execute,
    )
    return True


__all__ = [
    "EXECUTOR_VERSION",
    "RCM_VERSION",
    "job_run_root",
    "run_exact_draft",
    "run_exact_job",
]
