"""Owner-scoped, Web-JWT-only Survey aggregate routes."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

import structlog
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
from sanchezcloud_identity.models.user import UserRecord

from scholight.api.deps import get_current_user
from scholight.api.http_errors import http_error
from scholight.config import get_survey_public_mode, settings
from scholight.db.client import DBError
from scholight.db.queries_survey import (
    Survey,
    SurveyProgressSnapshot,
    SurveyQuotaExceededError,
    SurveyStateError,
    cancel_survey,
    create_survey,
    delete_survey,
    get_survey,
    get_survey_progress,
    set_survey_title_if_missing,
    start_survey,
)
from scholight.db.queries_survey_drafts import (
    SurveyDraft,
    SurveyDraftLimitError,
    create_manual_draft,
    list_survey_drafts,
    request_generated_draft,
)
from scholight.db.queries_survey_views import (
    SurveyArtifactReference,
    SurveyListView,
    SurveySummary,
    get_survey_artifact_reference,
    list_survey_summaries,
)
from scholight.survey.artifacts import (
    SurveyArtifactError,
    SurveyArtifactNotFoundError,
    SurveyArtifactStore,
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
from scholight.survey.report_pdf import ReportPdfError, render_report_pdf
from scholight.survey.title import generate_survey_title
from scholight.survey.wakeup import wake_survey_control

router = APIRouter()
logger = structlog.get_logger(__name__)


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
    notify_on_completion: bool = False


class SurveyResponse(BaseModel):
    id: UUID
    title: str
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


class SurveyQuotaResponse(BaseModel):
    daily_limit: int
    reserved: int
    succeeded: int
    remaining: int


class SurveySummaryResponse(BaseModel):
    id: UUID
    title: str
    status: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    latest_draft_revision: int | None
    error_code: str | None
    error_message: str | None
    progress: SurveyProgressResponse
    report_available: bool
    artifacts_available: bool


class SurveyListResponse(BaseModel):
    items: list[SurveySummaryResponse]
    quota: SurveyQuotaResponse
    next_cursor: str | None


class SurveyArtifactItemResponse(BaseModel):
    path: str
    size: int
    sha256: str
    content_type: str
    download_url: str


class SurveyArtifactsResponse(BaseModel):
    survey_id: UUID
    expires_at: datetime
    items: list[SurveyArtifactItemResponse]


def _survey_response(survey: Survey) -> SurveyResponse:
    return SurveyResponse(
        id=survey.id,
        title=survey.title or _fallback_title(survey.initial_request),
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
    if snapshot.cancel_requested_at is not None and snapshot.status == "running":
        stage = "cancelling"
    max_slots = (
        settings.survey_draft_global_concurrency
        if snapshot.queue_kind == "draft"
        else settings.survey_job_global_concurrency
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
    elapsed_start = snapshot.started_at
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


def _fallback_title(initial_request: str) -> str:
    first_line = next((line.strip() for line in initial_request.splitlines() if line.strip()), "")
    if len(first_line) <= 96:
        return first_line or "Untitled survey"
    return f"{first_line[:95].rstrip()}…"


def _encode_cursor(*, created_at: datetime, survey_id: UUID) -> str:
    payload = json.dumps(
        [created_at.isoformat(), str(survey_id)],
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str | None) -> tuple[datetime | None, UUID | None]:
    if value is None:
        return None, None
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if not isinstance(payload, list) or len(payload) != 2:
            raise ValueError
        created_at = datetime.fromisoformat(str(payload[0]).replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            raise ValueError
        return created_at, UUID(str(payload[1]))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise http_error(
            422,
            code="survey_cursor_invalid",
            message="The Survey pagination cursor is invalid.",
            retryable=False,
            retry_after=None,
        ) from exc


def _summary_response(summary: SurveySummary) -> SurveySummaryResponse:
    return SurveySummaryResponse(
        id=summary.id,
        title=summary.title or _fallback_title(summary.initial_request),
        status=summary.status,
        created_at=summary.created_at,
        updated_at=summary.updated_at,
        started_at=summary.started_at,
        finished_at=summary.finished_at,
        latest_draft_revision=summary.latest_draft_revision,
        error_code=summary.error_code,
        error_message=summary.error_message,
        progress=_progress_response(summary.progress),
        report_available=summary.report_available,
        artifacts_available=summary.artifacts_available,
    )


def _require_enabled() -> None:
    if get_survey_public_mode() != "all":
        raise http_error(
            404,
            code="survey_unavailable",
            message="Scholight Survey is not available yet.",
            retryable=False,
            retry_after=None,
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
    await wake_survey_control(reason="draft_submitted")
    if survey.title is None:
        title = await generate_survey_title(body.initial_request)
        if title is not None:
            try:
                titled_survey = await set_survey_title_if_missing(
                    survey_id=survey.id,
                    user_id=current_user.id,
                    title=title,
                )
                if titled_survey is not None:
                    survey = titled_survey
            except DBError as exc:
                logger.warning(
                    "survey_title_persist_failed",
                    survey_id=str(survey.id),
                    error_type=type(exc).__name__,
                )
    return _survey_response(survey)


@router.get("/surveys", response_model=SurveyListResponse)
async def surveys(
    view: SurveyListView = "all",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
    current_user: UserRecord = Depends(get_current_user),
) -> SurveyListResponse:
    _require_enabled()
    cursor_created_at, cursor_id = _decode_cursor(cursor)
    try:
        page = await list_survey_summaries(
            user_id=current_user.id,
            quota_date=datetime.now(UTC).date(),
            daily_limit=settings.survey_daily_limit,
            view=view,
            limit=limit,
            cursor_created_at=cursor_created_at,
            cursor_id=cursor_id,
        )
    except DBError as exc:
        raise _service_unavailable() from exc
    next_cursor = (
        _encode_cursor(created_at=page.items[-1].created_at, survey_id=page.items[-1].id)
        if page.has_more and page.items
        else None
    )
    return SurveyListResponse(
        items=[_summary_response(item) for item in page.items],
        quota=SurveyQuotaResponse(
            daily_limit=page.quota.daily_limit,
            reserved=page.quota.reserved,
            succeeded=page.quota.succeeded,
            remaining=page.quota.remaining,
        ),
        next_cursor=next_cursor,
    )


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


async def _artifact_reference(*, survey_id: UUID, user_id: int) -> SurveyArtifactReference:
    try:
        reference = await get_survey_artifact_reference(
            survey_id=survey_id,
            user_id=user_id,
        )
    except DBError as exc:
        raise _service_unavailable() from exc
    if reference is None:
        raise http_error(
            404,
            code="survey_not_found",
            message="This Survey no longer exists or is not available to this account.",
            retryable=False,
            retry_after=None,
        )
    return reference


def _artifact_store(reference: SurveyArtifactReference) -> SurveyArtifactStore:
    if reference.storage_bucket is None:
        raise http_error(
            409,
            code="survey_artifacts_not_available",
            message="This Survey does not have archived artifacts.",
            retryable=False,
            retry_after=None,
        )
    if reference.storage_bucket != settings.survey_s3_bucket:
        raise _artifact_unavailable()
    return SurveyArtifactStore(
        bucket=settings.survey_s3_bucket,
        endpoint_url=settings.survey_s3_endpoint_url,
        public_endpoint_url=settings.survey_s3_public_endpoint_url,
    )


def _require_archived(reference: SurveyArtifactReference, *, report: bool) -> str:
    if reference.survey_status == "archiving" or reference.job_status == "archiving":
        raise http_error(
            409,
            code="survey_archive_pending",
            message="Survey artifacts are still being archived.",
            retryable=True,
            retry_after=5,
        )
    if report and reference.survey_status != "succeeded":
        raise http_error(
            409,
            code="survey_report_not_available",
            message="A final report is available only after a Survey succeeds.",
            retryable=False,
            retry_after=None,
        )
    if (
        reference.job_status != "finished"
        or reference.job_id is None
        or reference.manifest_key is None
        or reference.storage_bucket is None
        or reference.storage_prefix is None
    ):
        code = "survey_report_not_available" if report else "survey_artifacts_not_available"
        message = (
            "This Survey does not have a final report."
            if report
            else "This Survey does not have archived artifacts."
        )
        raise http_error(
            409,
            code=code,
            message=message,
            retryable=False,
            retry_after=None,
        )
    expected_prefix = SurveyArtifactStore.prefix(
        user_id=reference.user_id,
        job_id=reference.job_id,
    )
    base_manifest = f"{expected_prefix}/manifest.json"
    overlay_pattern = re.compile(
        rf"{re.escape(expected_prefix)}/recoveries/[0-9a-f]{{64}}/manifest\.json"
    )
    if reference.storage_prefix != expected_prefix or not (
        reference.manifest_key == base_manifest
        or overlay_pattern.fullmatch(reference.manifest_key) is not None
    ):
        raise _artifact_unavailable()
    return reference.manifest_key


def _artifact_unavailable() -> HTTPException:
    return http_error(
        503,
        code="survey_artifact_service_unavailable",
        message="Survey artifacts are temporarily unavailable.",
        retryable=True,
        retry_after=5,
    )


@router.get("/surveys/{survey_id}/report", response_class=StreamingResponse)
async def survey_report(
    survey_id: UUID,
    current_user: UserRecord = Depends(get_current_user),
) -> StreamingResponse:
    _require_enabled()
    reference = await _artifact_reference(survey_id=survey_id, user_id=current_user.id)
    manifest_key = _require_archived(reference, report=True)
    try:
        stream = await _artifact_store(reference).open_artifact(
            manifest_key=manifest_key,
            path="run/08_survey.md",
        )
    except SurveyArtifactNotFoundError as exc:
        raise http_error(
            409,
            code="survey_report_not_available",
            message="This Survey does not have a final report.",
            retryable=False,
            retry_after=None,
        ) from exc
    except (SurveyArtifactError, BotoCoreError, ClientError) as exc:
        raise _artifact_unavailable() from exc
    if stream.content_type not in {"text/markdown", "text/plain"}:
        raise _artifact_unavailable()
    return StreamingResponse(
        stream.chunks(),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Cache-Control": "private, no-store",
            "ETag": f'"{stream.sha256}"',
            "Content-Length": str(stream.size),
        },
    )


@router.get(
    "/surveys/{survey_id}/download",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/zip": {}}}},
)
async def survey_report_download(
    survey_id: UUID,
    current_user: UserRecord = Depends(get_current_user),
) -> StreamingResponse:
    _require_enabled()
    reference = await _artifact_reference(survey_id=survey_id, user_id=current_user.id)
    manifest_key = _require_archived(reference, report=True)
    try:
        stream = await _artifact_store(reference).build_report_package(manifest_key=manifest_key)
    except SurveyArtifactNotFoundError as exc:
        raise http_error(
            409,
            code="survey_report_not_available",
            message="This Survey does not have a final report.",
            retryable=False,
            retry_after=None,
        ) from exc
    except (SurveyArtifactError, BotoCoreError, ClientError) as exc:
        raise _artifact_unavailable() from exc
    return StreamingResponse(
        stream.chunks(),
        media_type="application/zip",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": (f'attachment; filename="scholight-survey-{survey_id}.zip"'),
            "ETag": f'"{stream.sha256}"',
            "Content-Length": str(stream.size),
        },
    )


@router.get(
    "/surveys/{survey_id}/report.pdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def survey_report_pdf(
    survey_id: UUID,
    current_user: UserRecord = Depends(get_current_user),
) -> Response:
    """Stream the archived branded PDF, rendering it synchronously only as a fallback.

    The worker pre-renders the PDF at archive time; older archives without one
    fall back to an in-request render, which large reports cannot complete
    within the gateway timeout, so the stream is preferred whenever present.
    """
    _require_enabled()
    reference = await _artifact_reference(survey_id=survey_id, user_id=current_user.id)
    manifest_key = _require_archived(reference, report=True)
    store = _artifact_store(reference)
    try:
        pdf_stream = await store.open_artifact(
            manifest_key=manifest_key,
            path="run/08_survey.pdf",
        )
    except SurveyArtifactNotFoundError:
        pdf_stream = None
    except (SurveyArtifactError, BotoCoreError, ClientError) as exc:
        raise _artifact_unavailable() from exc
    if pdf_stream is not None and pdf_stream.content_type == "application/pdf":
        return StreamingResponse(
            pdf_stream.chunks(),
            media_type="application/pdf",
            headers={
                "Cache-Control": "private, no-store",
                "ETag": f'"{pdf_stream.sha256}"',
                "Content-Length": str(pdf_stream.size),
                "Content-Disposition": (f'attachment; filename="scholight-survey-{survey_id}.pdf"'),
            },
        )
    try:
        markdown_bytes, images = await store.open_report_assets(
            manifest_key=manifest_key,
        )
    except SurveyArtifactNotFoundError as exc:
        raise http_error(
            409,
            code="survey_report_not_available",
            message="This Survey does not have a final report.",
            retryable=False,
            retry_after=None,
        ) from exc
    except (SurveyArtifactError, BotoCoreError, ClientError) as exc:
        raise _artifact_unavailable() from exc
    survey = await _owned_survey(survey_id=survey_id, user_id=current_user.id)
    generated_on = (
        survey.finished_at.date() if survey.finished_at is not None else datetime.now(UTC).date()
    )
    try:
        markdown_text = markdown_bytes.decode("utf-8")
        pdf = await asyncio.to_thread(
            render_report_pdf,
            title=survey.title or _fallback_title(survey.initial_request),
            markdown_text=markdown_text,
            images=images,
            generated_on=generated_on,
        )
    except UnicodeDecodeError as exc:
        raise _artifact_unavailable() from exc
    except ReportPdfError as exc:
        logger.warning(
            "survey_report_pdf_failed",
            survey_id=str(survey_id),
            error_type=type(exc).__name__,
        )
        raise http_error(
            503,
            code="survey_report_pdf_unavailable",
            message="The Survey report PDF is temporarily unavailable.",
            retryable=True,
            retry_after=5,
        ) from exc
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="scholight-survey-{survey_id}.pdf"',
        },
    )


@router.get("/surveys/{survey_id}/artifacts", response_model=SurveyArtifactsResponse)
async def survey_artifacts(
    survey_id: UUID,
    current_user: UserRecord = Depends(get_current_user),
) -> SurveyArtifactsResponse:
    _require_enabled()
    reference = await _artifact_reference(survey_id=survey_id, user_id=current_user.id)
    manifest_key = _require_archived(reference, report=False)
    try:
        artifacts = await _artifact_store(reference).presigned_artifacts(
            manifest_key=manifest_key,
            expires_seconds=300,
        )
    except (SurveyArtifactError, BotoCoreError, ClientError) as exc:
        raise _artifact_unavailable() from exc
    return SurveyArtifactsResponse(
        survey_id=survey_id,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        items=[
            SurveyArtifactItemResponse(
                path=str(item["path"]),
                size=int(item["size"]),
                sha256=str(item["sha256"]),
                content_type=str(item["mime"]),
                download_url=str(item["url"]),
            )
            for item in artifacts
        ],
    )


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
    await wake_survey_control(reason="draft_revision_submitted")
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
                payload={
                    "survey_id": str(survey_id),
                    "notify_on_completion": body.notify_on_completion,
                },
            ),
            notify_on_completion=body.notify_on_completion,
        )
    except SurveyStateError as exc:
        raise _state_error(exc) from exc
    except DBError as exc:
        raise _service_unavailable() from exc
    await wake_survey_control(reason="survey_execution_submitted")
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


@router.delete("/surveys/{survey_id}", status_code=204, response_class=Response)
async def remove_survey(
    survey_id: UUID,
    current_user: UserRecord = Depends(get_current_user),
) -> Response:
    _require_enabled()
    try:
        await delete_survey(survey_id=survey_id, user_id=current_user.id)
    except SurveyStateError as exc:
        raise _state_error(exc) from exc
    except DBError as exc:
        raise _service_unavailable() from exc
    return Response(status_code=204)


__all__ = ["router"]
