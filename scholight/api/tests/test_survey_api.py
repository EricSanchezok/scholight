"""Web-JWT-only Survey API contract tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from cloud_auth.models.user import UserRecord
from fastapi import FastAPI

from scholight.api.deps import get_current_user
from scholight.config import settings
from scholight.db.queries_survey import SurveyJob, SurveyQuotaExceededError

pytestmark = pytest.mark.asyncio


def _job(*, job_id: UUID, status: str = "pending") -> SurveyJob:
    now = datetime.now(UTC)
    return SurveyJob(
        id=job_id,
        user_id=42,
        topic="retrieval augmented generation",
        status=status,  # type: ignore[arg-type]
        terminal_outcome=None,
        quota_date=date(2026, 7, 31),
        storage_prefix=None,
        manifest_key=None,
        error_code=None,
        error_message=None,
        lease_owner=None,
        lease_expires_at=None,
        heartbeat_at=None,
        archive_attempts=0,
        next_archive_at=None,
        created_at=now,
        started_at=None,
        finished_at=None,
    )


def _authenticate(app: FastAPI, user: UserRecord) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


async def test_survey_is_disabled_fail_closed(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
) -> None:
    _authenticate(api_app, active_user)
    settings.survey_enabled = False

    response = await api_client.get("/survey/jobs")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "survey_unavailable"


async def test_submit_reserves_one_user_daily_slot(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_enabled", True)
    monkeypatch.setattr(settings, "survey_daily_limit", 5)
    job_id = uuid4()
    with patch(
        "scholight.api.routes.survey.create_survey_job",
        new_callable=AsyncMock,
        return_value=_job(job_id=job_id),
    ) as create:
        response = await api_client.post(
            "/survey/jobs",
            json={"topic": "  retrieval augmented generation  "},
        )

    assert response.status_code == 201
    assert response.json()["id"] == str(job_id)
    call = create.await_args
    assert call is not None
    assert call.kwargs["user_id"] == active_user.id
    assert call.kwargs["topic"] == "retrieval augmented generation"
    assert call.kwargs["daily_limit"] == 5


async def test_survey_quota_has_stable_error(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_enabled", True)
    with patch(
        "scholight.api.routes.survey.create_survey_job",
        new_callable=AsyncMock,
        side_effect=SurveyQuotaExceededError,
    ):
        response = await api_client.post(
            "/survey/jobs",
            json={"topic": "retrieval augmented generation"},
        )

    assert response.status_code == 429
    assert response.json()["detail"] == {
        "code": "survey_quota_exceeded",
        "message": "Today's Scholight Survey allowance has been used.",
        "retryable": False,
    }


async def test_anonymous_cannot_submit_survey(
    api_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "survey_enabled", True)

    response = await api_client.post(
        "/survey/jobs",
        json={"topic": "retrieval augmented generation"},
    )

    assert response.status_code in {401, 403}


async def test_access_key_cannot_submit_survey(
    api_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "survey_enabled", True)

    response = await api_client.post(
        "/survey/jobs",
        headers={"Authorization": "Bearer sk_live_test-only"},
        json={"topic": "retrieval augmented generation"},
    )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "authentication_required"


async def test_running_job_cannot_be_deleted(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_enabled", True)
    job_id = uuid4()
    with patch(
        "scholight.api.routes.survey.get_survey_job",
        new_callable=AsyncMock,
        return_value=_job(job_id=job_id, status="running"),
    ):
        response = await api_client.delete(f"/survey/jobs/{job_id}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "survey_job_in_progress"


async def test_artifacts_fail_closed_on_cross_owner_manifest_key(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_enabled", True)
    job_id = uuid4()
    job = replace(
        _job(job_id=job_id, status="pending"),
        status="succeeded",
        terminal_outcome="succeeded",
        storage_prefix=f"surveys/v1/99/{job_id}",
        manifest_key=f"surveys/v1/99/{job_id}/manifest.json",
        finished_at=datetime.now(UTC),
    )
    with (
        patch(
            "scholight.api.routes.survey.get_survey_job",
            new_callable=AsyncMock,
            return_value=job,
        ),
        patch(
            "scholight.api.routes.survey.get_survey_artifact_store",
        ) as artifact_store,
    ):
        response = await api_client.get(f"/survey/jobs/{job_id}/artifacts")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "survey_service_unavailable"
    artifact_store.assert_not_called()
