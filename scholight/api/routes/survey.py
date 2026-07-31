"""Owner-scoped, Web-JWT-only Scholight Survey job routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from cloud_auth.models.user import UserRecord
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator

from scholight.api.deps import get_current_user
from scholight.api.http_errors import http_error
from scholight.config import settings
from scholight.db.client import DBError
from scholight.db.queries_survey import (
    SurveyJob,
    SurveyJobStateError,
    SurveyQuotaExceededError,
    create_survey_job,
    delete_pending_survey_job,
    delete_terminal_survey_job,
    get_survey_job,
    list_survey_jobs,
)
from scholight.survey.artifacts import SurveyArtifactError, SurveyArtifactStore

router = APIRouter()


class SurveyJobCreateRequest(BaseModel):
    topic: str

    @field_validator("topic")
    @classmethod
    def _topic_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Survey topic must not be blank")
        return normalized


class SurveyJobResponse(BaseModel):
    id: UUID
    topic: str
    status: str
    terminal_outcome: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class SurveyArtifactResponse(BaseModel):
    path: str
    size: int = Field(ge=0)
    sha256: str
    mime: str
    url: str


class SurveyArtifactsResponse(BaseModel):
    expires_in_seconds: int = 300
    items: list[SurveyArtifactResponse]


def _response(job: SurveyJob) -> SurveyJobResponse:
    return SurveyJobResponse(
        id=job.id,
        topic=job.topic,
        status=job.status,
        terminal_outcome=job.terminal_outcome,
        error_code=job.error_code,
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def _require_enabled() -> None:
    if not settings.survey_enabled:
        raise http_error(
            503,
            code="survey_unavailable",
            message="Scholight Survey is not available yet.",
            retryable=True,
            retry_after=60,
        )


def get_survey_artifact_store() -> SurveyArtifactStore:
    return SurveyArtifactStore(bucket=settings.survey_s3_bucket)


def _verified_manifest_key(job: SurveyJob) -> str:
    expected = f"{SurveyArtifactStore.prefix(user_id=job.user_id, job_id=job.id)}/manifest.json"
    if job.manifest_key != expected:
        raise SurveyArtifactError("Survey manifest ownership is invalid")
    return expected


def _service_unavailable() -> HTTPException:
    return http_error(
        503,
        code="survey_service_unavailable",
        message="Scholight Survey is temporarily unavailable.",
        retryable=True,
        retry_after=5,
    )


@router.post("/jobs", response_model=SurveyJobResponse, status_code=201)
async def submit_survey_job(
    body: SurveyJobCreateRequest,
    current_user: UserRecord = Depends(get_current_user),
) -> SurveyJobResponse:
    _require_enabled()
    try:
        job = await create_survey_job(
            job_id=uuid4(),
            user_id=current_user.id,
            topic=body.topic,
            quota_date=datetime.now(UTC).date(),
            daily_limit=settings.survey_daily_limit,
        )
    except SurveyQuotaExceededError as exc:
        raise http_error(
            429,
            code="survey_quota_exceeded",
            message="Today's Scholight Survey allowance has been used.",
            retryable=False,
            retry_after=None,
        ) from exc
    except DBError as exc:
        raise _service_unavailable() from exc
    return _response(job)


@router.get("/jobs", response_model=list[SurveyJobResponse])
async def survey_jobs(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    current_user: UserRecord = Depends(get_current_user),
) -> list[SurveyJobResponse]:
    _require_enabled()
    try:
        jobs = await list_survey_jobs(user_id=current_user.id, limit=limit)
    except DBError as exc:
        raise _service_unavailable() from exc
    return [_response(job) for job in jobs]


async def _owned_job(*, job_id: UUID, user_id: int) -> SurveyJob:
    try:
        job = await get_survey_job(job_id=job_id, user_id=user_id)
    except DBError as exc:
        raise _service_unavailable() from exc
    if job is None:
        raise http_error(
            404,
            code="survey_job_not_found",
            message="This Survey job no longer exists or is not available to this account.",
            retryable=False,
            retry_after=None,
        )
    return job


@router.get("/jobs/{job_id}", response_model=SurveyJobResponse)
async def survey_job(
    job_id: UUID,
    current_user: UserRecord = Depends(get_current_user),
) -> SurveyJobResponse:
    _require_enabled()
    return _response(await _owned_job(job_id=job_id, user_id=current_user.id))


@router.get("/jobs/{job_id}/artifacts", response_model=SurveyArtifactsResponse)
async def survey_artifacts(
    job_id: UUID,
    current_user: UserRecord = Depends(get_current_user),
) -> SurveyArtifactsResponse:
    _require_enabled()
    job = await _owned_job(job_id=job_id, user_id=current_user.id)
    if job.status not in {"succeeded", "failed"} or job.manifest_key is None:
        raise http_error(
            409,
            code="survey_artifacts_not_ready",
            message="Survey artifacts are still being prepared.",
            retryable=True,
            retry_after=5,
        )
    try:
        manifest_key = _verified_manifest_key(job)
        records = await get_survey_artifact_store().presigned_artifacts(
            manifest_key=manifest_key,
        )
    except SurveyArtifactError as exc:
        raise _service_unavailable() from exc
    return SurveyArtifactsResponse(items=[SurveyArtifactResponse(**record) for record in records])


@router.delete("/jobs/{job_id}", status_code=204)
async def delete_survey_job(
    job_id: UUID,
    current_user: UserRecord = Depends(get_current_user),
) -> Response:
    _require_enabled()
    job = await _owned_job(job_id=job_id, user_id=current_user.id)
    try:
        if job.status == "pending":
            deleted = await delete_pending_survey_job(
                job_id=job_id,
                user_id=current_user.id,
            )
        elif job.status in {"running", "archiving"}:
            raise http_error(
                409,
                code="survey_job_in_progress",
                message="A running or archiving Survey job cannot be deleted.",
                retryable=False,
                retry_after=None,
            )
        else:
            if job.manifest_key is None:
                raise SurveyArtifactError("Survey manifest is missing")
            manifest_key = _verified_manifest_key(job)
            artifact_store = get_survey_artifact_store()
            await artifact_store.delete_archive(
                manifest_key=manifest_key,
                preserve_manifest=True,
            )
            deleted = await delete_terminal_survey_job(
                job_id=job_id,
                user_id=current_user.id,
            )
            if deleted:
                await artifact_store.delete_manifest(manifest_key=manifest_key)
    except SurveyJobStateError as exc:
        raise http_error(
            409,
            code="survey_job_state_changed",
            message="The Survey job changed state. Refresh and try again.",
            retryable=True,
            retry_after=1,
        ) from exc
    except (DBError, SurveyArtifactError) as exc:
        raise _service_unavailable() from exc
    if not deleted:
        raise http_error(
            404,
            code="survey_job_not_found",
            message="This Survey job no longer exists.",
            retryable=False,
            retry_after=None,
        )
    return Response(status_code=204)


__all__ = ["get_survey_artifact_store", "router"]
