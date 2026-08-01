"""Web-JWT-only Survey aggregate API contract tests."""

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
from scholight.db.queries_survey import (
    Survey,
    SurveyProgressSnapshot,
    SurveyQuotaExceededError,
    SurveyStateError,
)
from scholight.db.queries_survey_drafts import SurveyDraft

pytestmark = pytest.mark.asyncio


def _survey(*, survey_id: UUID, status: str = "drafting") -> Survey:
    now = datetime.now(UTC)
    return Survey(
        id=survey_id,
        user_id=42,
        client_request_id=uuid4(),
        request_hash="0" * 64,
        initial_request="retrieval augmented generation",
        status=status,  # type: ignore[arg-type]
        quota_date=date(2026, 7, 31),
        quota_state="reserved",
        error_code=None,
        error_message=None,
        created_at=now,
        updated_at=now,
        started_at=None,
        finished_at=None,
    )


def _draft(*, survey_id: UUID, status: str = "queued") -> SurveyDraft:
    now = datetime.now(UTC)
    return SurveyDraft(
        id=uuid4(),
        survey_id=survey_id,
        user_id=42,
        revision=None,
        source="generated",
        user_message="Focus on evaluation methods",
        markdown=None,
        status=status,  # type: ignore[arg-type]
        based_on_revision=None,
        client_request_id=uuid4(),
        request_hash="1" * 64,
        error_code=None,
        error_message=None,
        lease_owner=None,
        lease_expires_at=None,
        heartbeat_at=None,
        queued_at=now,
        last_claim_at=None,
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

    response = await api_client.get("/surveys")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "survey_unavailable"


async def test_create_survey_reserves_slot_and_queues_initial_draft(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_enabled", True)
    monkeypatch.setattr(settings, "survey_daily_limit", 5)
    survey_id = uuid4()
    request_id = uuid4()
    with patch(
        "scholight.api.routes.survey.create_survey",
        new_callable=AsyncMock,
        return_value=_survey(survey_id=survey_id),
    ) as create:
        response = await api_client.post(
            "/surveys",
            json={
                "initial_request": "  retrieval augmented generation  ",
                "client_request_id": str(request_id),
            },
        )

    assert response.status_code == 201
    assert response.json()["id"] == str(survey_id)
    call = create.await_args
    assert call is not None
    assert call.kwargs["user_id"] == active_user.id
    assert call.kwargs["initial_request"] == "retrieval augmented generation"
    assert call.kwargs["client_request_id"] == request_id
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
        "scholight.api.routes.survey.create_survey",
        new_callable=AsyncMock,
        side_effect=SurveyQuotaExceededError,
    ):
        response = await api_client.post(
            "/surveys",
            json={
                "initial_request": "retrieval augmented generation",
                "client_request_id": str(uuid4()),
            },
        )

    assert response.status_code == 429
    assert response.json()["detail"] == {
        "code": "survey_quota_exceeded",
        "message": "Today's Scholight Survey allowance has been used.",
        "retryable": False,
    }


async def test_anonymous_and_access_key_cannot_create_survey(
    api_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "survey_enabled", True)
    body = {
        "initial_request": "retrieval augmented generation",
        "client_request_id": str(uuid4()),
    }

    anonymous = await api_client.post("/surveys", json=body)
    access_key = await api_client.post(
        "/surveys",
        headers={"Authorization": "Bearer sk_live_test-only"},
        json=body,
    )

    assert anonymous.status_code in {401, 403}
    assert access_key.status_code == 401
    assert access_key.json()["detail"]["code"] == "authentication_required"


async def test_revision_endpoint_queues_async_draft_for_owner(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_enabled", True)
    survey_id = uuid4()
    request_id = uuid4()
    with (
        patch(
            "scholight.api.routes.survey.get_survey",
            new_callable=AsyncMock,
            return_value=_survey(survey_id=survey_id),
        ),
        patch(
            "scholight.api.routes.survey.request_generated_draft",
            new_callable=AsyncMock,
            return_value=_draft(survey_id=survey_id),
        ) as revise,
    ):
        response = await api_client.post(
            f"/surveys/{survey_id}/drafts",
            json={
                "message": "  Focus on evaluation methods  ",
                "client_request_id": str(request_id),
            },
        )

    assert response.status_code == 201
    call = revise.await_args
    assert call is not None
    assert call.kwargs["user_message"] == "Focus on evaluation methods"
    assert call.kwargs["client_request_id"] == request_id


async def test_manual_draft_and_start_use_distinct_write_endpoints(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_enabled", True)
    survey_id = uuid4()
    owner = _survey(survey_id=survey_id)
    ready = replace(
        _draft(survey_id=survey_id, status="ready"),
        revision=1,
        markdown="# Approved scope",
    )
    with (
        patch(
            "scholight.api.routes.survey.get_survey",
            new_callable=AsyncMock,
            return_value=owner,
        ),
        patch(
            "scholight.api.routes.survey.create_manual_draft",
            new_callable=AsyncMock,
            return_value=ready,
        ),
        patch(
            "scholight.api.routes.survey.start_survey",
            new_callable=AsyncMock,
            return_value=_survey(survey_id=survey_id, status="queued"),
        ),
    ):
        manual = await api_client.post(
            f"/surveys/{survey_id}/drafts/manual",
            json={
                "markdown": "# Approved scope",
                "message": "Manual edit",
                "client_request_id": str(uuid4()),
            },
        )
        started = await api_client.post(
            f"/surveys/{survey_id}/start",
            json={"client_request_id": str(uuid4())},
        )

    assert manual.status_code == 201
    assert manual.json()["markdown"] == "# Approved scope"
    assert started.status_code == 200
    assert started.json()["status"] == "queued"


async def test_manual_draft_accepts_full_one_mib_payload(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_enabled", True)
    survey_id = uuid4()
    markdown = "x" * (1024 * 1024)
    ready = replace(
        _draft(survey_id=survey_id, status="ready"),
        revision=1,
        markdown=markdown,
    )
    with (
        patch(
            "scholight.api.routes.survey.get_survey",
            new_callable=AsyncMock,
            return_value=_survey(survey_id=survey_id),
        ),
        patch(
            "scholight.api.routes.survey.create_manual_draft",
            new_callable=AsyncMock,
            return_value=ready,
        ) as create,
    ):
        response = await api_client.post(
            f"/surveys/{survey_id}/drafts/manual",
            json={
                "markdown": markdown,
                "message": "Manual edit",
                "client_request_id": str(uuid4()),
            },
        )

    assert response.status_code == 201
    assert create.await_args is not None
    assert create.await_args.kwargs["markdown"] == markdown


async def test_cross_owner_survey_is_not_exposed(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_enabled", True)
    with patch(
        "scholight.api.routes.survey.get_survey",
        new_callable=AsyncMock,
        return_value=None,
    ):
        response = await api_client.get(f"/surveys/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "survey_not_found"


async def test_survey_disappearing_during_write_still_returns_not_found(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_enabled", True)
    survey_id = uuid4()
    with (
        patch(
            "scholight.api.routes.survey.get_survey",
            new_callable=AsyncMock,
            return_value=_survey(survey_id=survey_id),
        ),
        patch(
            "scholight.api.routes.survey.request_generated_draft",
            new_callable=AsyncMock,
            side_effect=SurveyStateError("Survey not found", code="survey_not_found"),
        ),
    ):
        response = await api_client.post(
            f"/surveys/{survey_id}/drafts",
            json={
                "message": "Focus on evaluation methods",
                "client_request_id": str(uuid4()),
            },
        )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "survey_not_found"


async def test_progress_endpoint_returns_stable_public_milestone(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_enabled", True)
    survey_id = uuid4()
    now = datetime.now(UTC)
    snapshot = SurveyProgressSnapshot(
        survey_id=survey_id,
        status="running",
        execution_stage="reviewing_evidence",
        queue_kind=None,
        queue_position=None,
        queued_at=None,
        running_slots=1,
        started_at=now,
        finished_at=None,
        last_activity_at=now,
    )
    with patch(
        "scholight.api.routes.survey.get_survey_progress",
        new_callable=AsyncMock,
        return_value=snapshot,
    ) as get_progress:
        response = await api_client.get(f"/surveys/{survey_id}/progress")

    assert response.status_code == 200
    assert response.json() == {
        "survey_id": str(survey_id),
        "status": "running",
        "stage": "reviewing_evidence",
        "percent": 55,
        "step": 3,
        "total_steps": 8,
        "queue": None,
        "elapsed_seconds": 0,
        "started_at": now.isoformat().replace("+00:00", "Z"),
        "finished_at": None,
        "last_activity_at": now.isoformat().replace("+00:00", "Z"),
    }
    call = get_progress.await_args
    assert call is not None
    assert call.kwargs["user_id"] == active_user.id


async def test_progress_endpoint_hides_cross_owner_survey(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_enabled", True)
    with patch(
        "scholight.api.routes.survey.get_survey_progress",
        new_callable=AsyncMock,
        return_value=None,
    ):
        response = await api_client.get(f"/surveys/{uuid4()}/progress")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "survey_not_found"
