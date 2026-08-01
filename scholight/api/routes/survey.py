"""Owner-scoped, Web-JWT-only Survey aggregate routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from cloud_auth.models.user import UserRecord
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator

from scholight.api.deps import get_current_user
from scholight.api.http_errors import http_error
from scholight.config import settings
from scholight.db.client import DBError
from scholight.db.queries_survey import (
    Survey,
    SurveyProgressSnapshot,
    SurveyQuotaExceededError,
    SurveyStateError,
    cancel_survey,
    create_survey,
    get_survey,
    get_survey_progress,
    list_surveys,
    start_survey,
)
from scholight.db.queries_survey_drafts import (
    SurveyDraft,
    SurveyDraftLimitError,
    create_manual_draft,
    list_survey_drafts,
    request_generated_draft,
)
from scholight.survey.contracts import (
    INITIAL_REQUEST_MAX_BYTES,
    MANUAL_DRAFT_MAX_BYTES,
    REVISION_MESSAGE_MAX_BYTES,
    canonical_request_hash,
    utf8_size,
)
from scholight.survey.progress import (
    TOTAL_PROGRESS_STEPS,
    PublicProgressStage,
    present_progress,
)

router = APIRouter()


class SurveyCreateRequest(BaseModel):
    initial_request: str
    client_request_id: UUID

    @field_validator("initial_request")
    @classmethod
    def _request_not_blank(cls, value: str) -> str:
        if not (normalized := value.strip()):
            raise ValueError("Survey request must not be blank")
        if utf8_size(normalized) > INITIAL_REQUEST_MAX_BYTES:
            raise ValueError("Survey request must not exceed 64 KiB")
        return normalized


class DraftCreateRequest(BaseModel):
    message: str
    client_request_id: UUID

    @field_validator("message")
    @classmethod
    def _message_not_blank(cls, value: str) -> str:
        if not (normalized := value.strip()):
            raise ValueError("Draft revision message must not be blank")
        if utf8_size(normalized) > REVISION_MESSAGE_MAX_BYTES:
            raise ValueError("Draft revision message must not exceed 64 KiB")
        return normalized


class ManualDraftCreateRequest(BaseModel):
    markdown: str
    client_request_id: UUID
    message: str = "Manual Draft revision"

    @field_validator("markdown")
    @classmethod
    def _draft_not_blank(cls, value: str) -> str:
        if not (normalized := value.strip()):
            raise ValueError("Manual Draft text must not be blank")
        if utf8_size(normalized) > MANUAL_DRAFT_MAX_BYTES:
            raise ValueError("Manual Draft must not exceed 1 MiB")
        return normalized

    @field_validator("message")
    @classmethod
    def _message_not_blank(cls, value: str) -> str:
        if not (normalized := value.strip()):
            raise ValueError("Manual Draft message must not be blank")
        if utf8_size(normalized) > REVISION_MESSAGE_MAX_BYTES:
            raise ValueError("Manual Draft message must not exceed 64 KiB")
        return normalized


class SurveyActionRequest(BaseModel):
    client_request_id: UUID


class SurveyResponse(BaseModel):
    id: UUID
    initial_request: str
    status: str
    quota_state: str
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class SurveyDraftResponse(BaseModel):
    id: UUID
    revision: int | None
    source: str
    user_message: str
    markdown: str | None
    status: str
    based_on_revision: int | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class SurveyProgressResponse(BaseModel):
    survey_id: UUID
    status: str
    stage: PublicProgressStage
    percent: int
    step: int
    total_steps: int
    queue: SurveyQueueResponse | None
    elapsed_seconds: int
    started_at: datetime | None
    finished_at: datetime | None
    last_activity_at: datetime


class SurveyQueueResponse(BaseModel):
    kind: str
    position: int
    queued_at: datetime
    running_slots: int
    max_slots: int


def _survey_response(survey: Survey) -> SurveyResponse:
    return SurveyResponse(
        id=survey.id,
        initial_request=survey.initial_request,
        status=survey.status,
        quota_state=survey.quota_state,
        error_code=survey.error_code,
        error_message=survey.error_message,
        created_at=survey.created_at,
        updated_at=survey.updated_at,
        started_at=survey.started_at,
        finished_at=survey.finished_at,
    )


def _draft_response(draft: SurveyDraft) -> SurveyDraftResponse:
    return SurveyDraftResponse(
        id=draft.id,
        revision=draft.revision,
        source=draft.source,
        user_message=draft.user_message,
        markdown=draft.markdown,
        status=draft.status,
        based_on_revision=draft.based_on_revision,
        error_code=draft.error_code,
        error_message=draft.error_message,
        created_at=draft.created_at,
        started_at=draft.started_at,
        finished_at=draft.finished_at,
    )


def _progress_response(snapshot: SurveyProgressSnapshot) -> SurveyProgressResponse:
    stage, percent, step = present_progress(
        survey_status=snapshot.status,
        execution_stage=snapshot.execution_stage,
    )
    if snapshot.queue_kind == "draft" and snapshot.queue_position is not None:
        stage = "waiting_for_draft"
    max_slots = (
        settings.survey_draft_concurrency
        if snapshot.queue_kind == "draft"
        else settings.survey_job_concurrency
    )
    queue = (
        SurveyQueueResponse(
            kind=snapshot.queue_kind,
            position=snapshot.queue_position,
            queued_at=snapshot.queued_at,
            running_slots=snapshot.running_slots,
            max_slots=max_slots,
        )
        if snapshot.queue_kind is not None
        and snapshot.queue_position is not None
        and snapshot.queued_at is not None
        else None
    )
    elapsed_start = snapshot.started_at if snapshot.status == "running" else None
    elapsed_seconds = (
        max(0, int(((snapshot.finished_at or datetime.now(UTC)) - elapsed_start).total_seconds()))
        if elapsed_start is not None
        else 0
    )
    return SurveyProgressResponse(
        survey_id=snapshot.survey_id,
        status=snapshot.status,
        stage=stage,
        percent=percent,
        step=step,
        total_steps=TOTAL_PROGRESS_STEPS,
        queue=queue,
        elapsed_seconds=elapsed_seconds,
        started_at=snapshot.started_at,
        finished_at=snapshot.finished_at,
        last_activity_at=snapshot.last_activity_at,
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


def _service_unavailable() -> HTTPException:
    return http_error(
        503,
        code="survey_service_unavailable",
        message="Scholight Survey is temporarily unavailable.",
        retryable=True,
        retry_after=5,
    )


def _state_error(exc: SurveyStateError) -> HTTPException:
    message = str(exc)
    code = exc.code
    if isinstance(exc, SurveyDraftLimitError):
        message = "This Survey already has the maximum of 10 Draft revisions."
    status_code = 404 if code == "survey_not_found" else 409
    return http_error(status_code, code=code, message=message, retryable=False, retry_after=None)


async def _owned_survey(*, survey_id: UUID, user_id: int) -> Survey:
    try:
        survey = await get_survey(survey_id=survey_id, user_id=user_id)
    except DBError as exc:
        raise _service_unavailable() from exc
    if survey is None:
        raise http_error(
            404,
            code="survey_not_found",
            message="This Survey no longer exists or is not available to this account.",
            retryable=False,
            retry_after=None,
        )
    return survey


@router.post("/surveys", response_model=SurveyResponse, status_code=201)
async def submit_survey(
    body: SurveyCreateRequest,
    current_user: UserRecord = Depends(get_current_user),
) -> SurveyResponse:
    _require_enabled()
    try:
        survey = await create_survey(
            survey_id=uuid4(),
            draft_id=uuid4(),
            user_id=current_user.id,
            initial_request=body.initial_request,
            client_request_id=body.client_request_id,
            request_hash=canonical_request_hash(
                operation="create_survey",
                payload={"initial_request": body.initial_request},
            ),
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
    return _survey_response(survey)


@router.get("/surveys", response_model=list[SurveyResponse])
async def surveys(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    current_user: UserRecord = Depends(get_current_user),
) -> list[SurveyResponse]:
    _require_enabled()
    try:
        rows = await list_surveys(user_id=current_user.id, limit=limit)
    except DBError as exc:
        raise _service_unavailable() from exc
    return [_survey_response(row) for row in rows]


@router.get("/surveys/{survey_id}", response_model=SurveyResponse)
async def survey(
    survey_id: UUID,
    current_user: UserRecord = Depends(get_current_user),
) -> SurveyResponse:
    _require_enabled()
    return _survey_response(await _owned_survey(survey_id=survey_id, user_id=current_user.id))


@router.get("/surveys/{survey_id}/progress", response_model=SurveyProgressResponse)
async def survey_progress(
    survey_id: UUID,
    current_user: UserRecord = Depends(get_current_user),
) -> SurveyProgressResponse:
    _require_enabled()
    try:
        snapshot = await get_survey_progress(survey_id=survey_id, user_id=current_user.id)
    except DBError as exc:
        raise _service_unavailable() from exc
    if snapshot is None:
        raise http_error(
            404,
            code="survey_not_found",
            message="This Survey no longer exists or is not available to this account.",
            retryable=False,
            retry_after=None,
        )
    return _progress_response(snapshot)


@router.get("/surveys/{survey_id}/drafts", response_model=list[SurveyDraftResponse])
async def survey_drafts(
    survey_id: UUID,
    current_user: UserRecord = Depends(get_current_user),
) -> list[SurveyDraftResponse]:
    _require_enabled()
    await _owned_survey(survey_id=survey_id, user_id=current_user.id)
    try:
        rows = await list_survey_drafts(survey_id=survey_id, user_id=current_user.id)
    except DBError as exc:
        raise _service_unavailable() from exc
    return [_draft_response(row) for row in rows]


@router.post("/surveys/{survey_id}/drafts", response_model=SurveyDraftResponse, status_code=201)
async def revise_survey_draft(
    survey_id: UUID,
    body: DraftCreateRequest,
    current_user: UserRecord = Depends(get_current_user),
) -> SurveyDraftResponse:
    _require_enabled()
    await _owned_survey(survey_id=survey_id, user_id=current_user.id)
    try:
        draft = await request_generated_draft(
            survey_id=survey_id,
            user_id=current_user.id,
            draft_id=uuid4(),
            client_request_id=body.client_request_id,
            request_hash=canonical_request_hash(
                operation="generate_draft",
                payload={"survey_id": str(survey_id), "message": body.message},
            ),
            user_message=body.message,
        )
    except SurveyStateError as exc:
        raise _state_error(exc) from exc
    except DBError as exc:
        raise _service_unavailable() from exc
    return _draft_response(draft)


@router.post(
    "/surveys/{survey_id}/drafts/manual",
    response_model=SurveyDraftResponse,
    status_code=201,
)
async def add_manual_survey_draft(
    survey_id: UUID,
    body: ManualDraftCreateRequest,
    current_user: UserRecord = Depends(get_current_user),
) -> SurveyDraftResponse:
    _require_enabled()
    await _owned_survey(survey_id=survey_id, user_id=current_user.id)
    try:
        draft = await create_manual_draft(
            survey_id=survey_id,
            user_id=current_user.id,
            draft_id=uuid4(),
            client_request_id=body.client_request_id,
            request_hash=canonical_request_hash(
                operation="manual_draft",
                payload={
                    "survey_id": str(survey_id),
                    "message": body.message,
                    "markdown": body.markdown,
                },
            ),
            user_message=body.message,
            markdown=body.markdown,
        )
    except SurveyStateError as exc:
        raise _state_error(exc) from exc
    except DBError as exc:
        raise _service_unavailable() from exc
    return _draft_response(draft)


@router.post("/surveys/{survey_id}/start", response_model=SurveyResponse)
async def start_survey_execution(
    survey_id: UUID,
    body: SurveyActionRequest,
    current_user: UserRecord = Depends(get_current_user),
) -> SurveyResponse:
    _require_enabled()
    await _owned_survey(survey_id=survey_id, user_id=current_user.id)
    try:
        updated = await start_survey(
            survey_id=survey_id,
            user_id=current_user.id,
            job_id=uuid4(),
            client_request_id=body.client_request_id,
            request_hash=canonical_request_hash(
                operation="start_survey",
                payload={"survey_id": str(survey_id)},
            ),
        )
    except SurveyStateError as exc:
        raise _state_error(exc) from exc
    except DBError as exc:
        raise _service_unavailable() from exc
    return _survey_response(updated)


@router.post("/surveys/{survey_id}/cancel", response_model=SurveyResponse)
async def cancel_survey_request(
    survey_id: UUID,
    current_user: UserRecord = Depends(get_current_user),
) -> SurveyResponse:
    _require_enabled()
    await _owned_survey(survey_id=survey_id, user_id=current_user.id)
    try:
        updated = await cancel_survey(survey_id=survey_id, user_id=current_user.id)
    except SurveyStateError as exc:
        raise _state_error(exc) from exc
    except DBError as exc:
        raise _service_unavailable() from exc
    return _survey_response(updated)


__all__ = ["router"]
