"""Web-JWT-only Survey aggregate API contract tests."""

from __future__ import annotations

import io
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sanchezcloud_identity.models.user import UserRecord

from scholight.api.deps import get_current_user
from scholight.api.routes.survey import _artifact_store
from scholight.config import settings
from scholight.db.queries_survey import (
    Survey,
    SurveyProgressSnapshot,
    SurveyQuotaExceededError,
    SurveyStateError,
)
from scholight.db.queries_survey_drafts import SurveyDraft
from scholight.db.queries_survey_views import (
    SurveyArtifactReference,
    SurveyQuotaSnapshot,
    SurveySummary,
    SurveySummaryPage,
)
from scholight.survey.artifacts import SurveyArtifactStream

pytestmark = pytest.mark.asyncio


def _survey(*, survey_id: UUID, status: str = "drafting") -> Survey:
    now = datetime.now(UTC)
    return Survey(
        id=survey_id,
        user_id=42,
        client_request_id=uuid4(),
        request_hash="0" * 64,
        initial_request="retrieval augmented generation",
        title=None,
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
    settings.survey_runtime_enabled = False
    settings.survey_public_mode = "off"

    response = await api_client.get("/surveys")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "survey_unavailable"


async def test_create_survey_reserves_slot_and_queues_initial_draft(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_runtime_enabled", True)
    monkeypatch.setattr(settings, "survey_public_mode", "all")
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


async def test_create_survey_persists_generated_navigation_title(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_runtime_enabled", True)
    monkeypatch.setattr(settings, "survey_public_mode", "all")
    survey_id = uuid4()
    untitled = _survey(survey_id=survey_id)
    titled = replace(untitled, title="RAG evaluation methods")
    with (
        patch(
            "scholight.api.routes.survey.create_survey",
            new_callable=AsyncMock,
            return_value=untitled,
        ),
        patch(
            "scholight.api.routes.survey.generate_survey_title",
            new_callable=AsyncMock,
            return_value="RAG evaluation methods",
        ),
        patch(
            "scholight.api.routes.survey.set_survey_title_if_missing",
            new_callable=AsyncMock,
            return_value=titled,
        ),
    ):
        response = await api_client.post(
            "/surveys",
            json={
                "initial_request": "Compare retrieval-augmented generation evaluation methods",
                "client_request_id": str(uuid4()),
            },
        )

    assert response.json()["title"] == "RAG evaluation methods"


async def test_survey_quota_has_stable_error(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_runtime_enabled", True)
    monkeypatch.setattr(settings, "survey_public_mode", "all")
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
    monkeypatch.setattr(settings, "survey_runtime_enabled", True)
    monkeypatch.setattr(settings, "survey_public_mode", "all")
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
    monkeypatch.setattr(settings, "survey_runtime_enabled", True)
    monkeypatch.setattr(settings, "survey_public_mode", "all")
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
    monkeypatch.setattr(settings, "survey_runtime_enabled", True)
    monkeypatch.setattr(settings, "survey_public_mode", "all")
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
        ) as start,
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
            json={"client_request_id": str(uuid4()), "notify_on_completion": True},
        )

    assert manual.status_code == 201
    assert manual.json()["markdown"] == "# Approved scope"
    assert started.status_code == 200
    assert started.json()["status"] == "queued"
    call = start.await_args
    assert call is not None
    assert call.kwargs["notify_on_completion"] is True


async def test_start_survey_defaults_notification_preference_off_for_old_clients(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_runtime_enabled", True)
    monkeypatch.setattr(settings, "survey_public_mode", "all")
    survey_id = uuid4()
    with (
        patch(
            "scholight.api.routes.survey.get_survey",
            new_callable=AsyncMock,
            return_value=_survey(survey_id=survey_id),
        ),
        patch(
            "scholight.api.routes.survey.start_survey",
            new_callable=AsyncMock,
            return_value=_survey(survey_id=survey_id, status="queued"),
        ) as start,
    ):
        response = await api_client.post(
            f"/surveys/{survey_id}/start",
            json={"client_request_id": str(uuid4())},
        )

    assert response.status_code == 200
    call = start.await_args
    assert call is not None
    assert call.kwargs["notify_on_completion"] is False


async def test_manual_draft_accepts_full_one_mib_payload(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_runtime_enabled", True)
    monkeypatch.setattr(settings, "survey_public_mode", "all")
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
    monkeypatch.setattr(settings, "survey_runtime_enabled", True)
    monkeypatch.setattr(settings, "survey_public_mode", "all")
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
    monkeypatch.setattr(settings, "survey_runtime_enabled", True)
    monkeypatch.setattr(settings, "survey_public_mode", "all")
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
    monkeypatch.setattr(settings, "survey_runtime_enabled", True)
    monkeypatch.setattr(settings, "survey_public_mode", "all")
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
    monkeypatch.setattr(settings, "survey_runtime_enabled", True)
    monkeypatch.setattr(settings, "survey_public_mode", "all")
    with patch(
        "scholight.api.routes.survey.get_survey_progress",
        new_callable=AsyncMock,
        return_value=None,
    ):
        response = await api_client.get(f"/surveys/{uuid4()}/progress")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "survey_not_found"


async def test_survey_list_returns_aggregate_projection_and_quota(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_runtime_enabled", True)
    monkeypatch.setattr(settings, "survey_public_mode", "all")
    monkeypatch.setattr(settings, "survey_daily_limit", 5)
    survey_id = uuid4()
    now = datetime.now(UTC)
    summary = SurveySummary(
        id=survey_id,
        title=None,
        initial_request="A focused topic\nwith details",
        status="queued",
        created_at=now,
        updated_at=now,
        started_at=None,
        finished_at=None,
        latest_draft_revision=2,
        error_code="survey_outline_metadata_invalid",
        error_message="Survey research finished, but the final report could not be assembled.",
        progress=SurveyProgressSnapshot(
            survey_id=survey_id,
            status="queued",
            execution_stage="planning",
            queue_kind="survey",
            queue_position=3,
            queued_at=now,
            running_slots=2,
            started_at=None,
            finished_at=None,
            last_activity_at=now,
        ),
        report_available=False,
        artifacts_available=False,
    )
    page = SurveySummaryPage(
        items=(summary,),
        quota=SurveyQuotaSnapshot(daily_limit=5, reserved=1, succeeded=2),
        has_more=True,
    )
    with patch(
        "scholight.api.routes.survey.list_survey_summaries",
        new_callable=AsyncMock,
        return_value=page,
    ) as list_summaries:
        response = await api_client.get("/surveys?view=active&limit=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"][0]["title"] == "A focused topic"
    assert payload["items"][0]["progress"]["stage"] == "waiting_for_execution"
    assert payload["items"][0]["progress"]["queue"]["position"] == 3
    assert payload["items"][0]["error_code"] == "survey_outline_metadata_invalid"
    assert payload["items"][0]["error_message"] == (
        "Survey research finished, but the final report could not be assembled."
    )
    assert payload["quota"] == {
        "daily_limit": 5,
        "reserved": 1,
        "succeeded": 2,
        "remaining": 2,
    }
    assert payload["next_cursor"] is not None
    assert list_summaries.await_count == 1


async def test_progress_reports_cancelling_and_terminal_elapsed_time(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_runtime_enabled", True)
    monkeypatch.setattr(settings, "survey_public_mode", "all")
    survey_id = uuid4()
    started = datetime.now(UTC) - timedelta(seconds=73)
    cancelling = SurveyProgressSnapshot(
        survey_id=survey_id,
        status="running",
        execution_stage="writing_report",
        queue_kind=None,
        queue_position=None,
        queued_at=None,
        running_slots=1,
        started_at=started,
        finished_at=None,
        last_activity_at=started,
        cancel_requested_at=datetime.now(UTC),
    )
    terminal = replace(
        cancelling,
        status="cancelled",
        finished_at=started + timedelta(seconds=42),
        cancel_requested_at=None,
    )
    with patch(
        "scholight.api.routes.survey.get_survey_progress",
        new_callable=AsyncMock,
        side_effect=[cancelling, terminal],
    ):
        running_response = await api_client.get(f"/surveys/{survey_id}/progress")
        terminal_response = await api_client.get(f"/surveys/{survey_id}/progress")

    assert running_response.json()["stage"] == "cancelling"
    assert running_response.json()["elapsed_seconds"] >= 73
    assert terminal_response.json()["stage"] == "cancelled"
    assert terminal_response.json()["elapsed_seconds"] == 42


async def test_report_streams_manifest_authorized_markdown_for_owner(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_runtime_enabled", True)
    monkeypatch.setattr(settings, "survey_public_mode", "all")
    survey_id = uuid4()
    job_id = uuid4()
    storage_prefix = f"surveys/v1/{active_user.id}/{job_id}"
    reference = SurveyArtifactReference(
        survey_id=survey_id,
        user_id=active_user.id,
        job_id=job_id,
        survey_status="succeeded",
        job_status="finished",
        terminal_outcome="succeeded",
        storage_bucket="private-bucket",
        storage_prefix=storage_prefix,
        manifest_key=f"{storage_prefix}/manifest.json",
    )
    stream = SurveyArtifactStream(
        path="run/08_survey.md",
        size=8,
        sha256="a" * 64,
        content_type="text/markdown",
        _body=io.BytesIO(b"# Report"),
    )
    store = AsyncMock()
    store.open_artifact.return_value = stream
    with (
        patch(
            "scholight.api.routes.survey.get_survey_artifact_reference",
            new_callable=AsyncMock,
            return_value=reference,
        ),
        patch("scholight.api.routes.survey._artifact_store", return_value=store),
    ):
        response = await api_client.get(f"/surveys/{survey_id}/report")

    assert response.status_code == 200
    assert response.text == "# Report"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["etag"] == f'"{"a" * 64}"'
    store.open_artifact.assert_awaited_once_with(
        manifest_key=f"{storage_prefix}/manifest.json",
        path="run/08_survey.md",
    )


async def test_report_download_streams_owner_scoped_zip_package(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_runtime_enabled", True)
    monkeypatch.setattr(settings, "survey_public_mode", "all")
    survey_id = uuid4()
    job_id = uuid4()
    storage_prefix = f"surveys/v1/{active_user.id}/{job_id}"
    reference = SurveyArtifactReference(
        survey_id=survey_id,
        user_id=active_user.id,
        job_id=job_id,
        survey_status="succeeded",
        job_status="finished",
        terminal_outcome="succeeded",
        storage_bucket="private-bucket",
        storage_prefix=storage_prefix,
        manifest_key=f"{storage_prefix}/manifest.json",
    )
    stream = SurveyArtifactStream(
        path="scholight-survey.zip",
        size=7,
        sha256="b" * 64,
        content_type="application/zip",
        _body=io.BytesIO(b"package"),
    )
    store = AsyncMock()
    store.build_report_package.return_value = stream
    with (
        patch(
            "scholight.api.routes.survey.get_survey_artifact_reference",
            new_callable=AsyncMock,
            return_value=reference,
        ),
        patch("scholight.api.routes.survey._artifact_store", return_value=store),
    ):
        response = await api_client.get(f"/surveys/{survey_id}/download")

    assert response.status_code == 200
    assert response.content == b"package"
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="scholight-survey-{survey_id}.zip"'
    )
    store.build_report_package.assert_awaited_once_with(
        manifest_key=f"{storage_prefix}/manifest.json"
    )


async def test_artifacts_hide_storage_keys_and_use_short_lived_urls(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_runtime_enabled", True)
    monkeypatch.setattr(settings, "survey_public_mode", "all")
    survey_id = uuid4()
    job_id = uuid4()
    storage_prefix = f"surveys/v1/{active_user.id}/{job_id}"
    reference = SurveyArtifactReference(
        survey_id=survey_id,
        user_id=active_user.id,
        job_id=job_id,
        survey_status="failed",
        job_status="finished",
        terminal_outcome="failed",
        storage_bucket="private-bucket",
        storage_prefix=storage_prefix,
        manifest_key=f"{storage_prefix}/manifest.json",
    )
    store = AsyncMock()
    store.presigned_artifacts.return_value = [
        {
            "path": "run/cards/paper.md",
            "key": "secret-s3-key",
            "size": 12,
            "sha256": "b" * 64,
            "mime": "text/markdown",
            "url": "https://signed.invalid/download",
        }
    ]
    with (
        patch(
            "scholight.api.routes.survey.get_survey_artifact_reference",
            new_callable=AsyncMock,
            return_value=reference,
        ),
        patch("scholight.api.routes.survey._artifact_store", return_value=store),
    ):
        response = await api_client.get(f"/surveys/{survey_id}/artifacts")

    assert response.status_code == 200
    assert response.json()["items"] == [
        {
            "path": "run/cards/paper.md",
            "size": 12,
            "sha256": "b" * 64,
            "content_type": "text/markdown",
            "download_url": "https://signed.invalid/download",
        }
    ]
    assert "private-bucket" not in response.text
    assert "secret-s3-key" not in response.text
    store.presigned_artifacts.assert_awaited_once_with(
        manifest_key=f"{storage_prefix}/manifest.json",
        expires_seconds=300,
    )


async def test_artifacts_sign_with_browser_facing_storage_endpoint(
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "survey_s3_bucket", "private-bucket")
    monkeypatch.setattr(settings, "survey_s3_endpoint_url", "http://minio:9000")
    monkeypatch.setattr(
        settings,
        "survey_s3_public_endpoint_url",
        "http://127.0.0.1:9000",
    )
    survey_id = uuid4()
    job_id = uuid4()
    storage_prefix = f"surveys/v1/{active_user.id}/{job_id}"
    reference = SurveyArtifactReference(
        survey_id=survey_id,
        user_id=active_user.id,
        job_id=job_id,
        survey_status="failed",
        job_status="finished",
        terminal_outcome="failed",
        storage_bucket="private-bucket",
        storage_prefix=storage_prefix,
        manifest_key=f"{storage_prefix}/manifest.json",
    )
    store = AsyncMock()
    with patch(
        "scholight.api.routes.survey.SurveyArtifactStore",
        return_value=store,
    ) as artifact_store:
        result = _artifact_store(reference)

    assert result is store
    artifact_store.assert_called_once_with(
        bucket="private-bucket",
        endpoint_url="http://minio:9000",
        public_endpoint_url="http://127.0.0.1:9000",
    )


async def test_archive_pending_is_retryable_without_opening_s3(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_runtime_enabled", True)
    monkeypatch.setattr(settings, "survey_public_mode", "all")
    survey_id = uuid4()
    reference = SurveyArtifactReference(
        survey_id=survey_id,
        user_id=active_user.id,
        job_id=uuid4(),
        survey_status="archiving",
        job_status="archiving",
        terminal_outcome="succeeded",
        storage_bucket="private-bucket",
        storage_prefix="hidden-prefix",
        manifest_key=None,
    )
    with patch(
        "scholight.api.routes.survey.get_survey_artifact_reference",
        new_callable=AsyncMock,
        return_value=reference,
    ):
        response = await api_client.get(f"/surveys/{survey_id}/artifacts")

    assert response.status_code == 409
    assert response.headers["retry-after"] == "5"
    assert response.json()["detail"]["code"] == "survey_archive_pending"


async def test_artifacts_reject_database_bucket_outside_runtime_configuration(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_runtime_enabled", True)
    monkeypatch.setattr(settings, "survey_public_mode", "all")
    monkeypatch.setattr(settings, "survey_s3_bucket", "configured-bucket")
    survey_id = uuid4()
    job_id = uuid4()
    storage_prefix = f"surveys/v1/{active_user.id}/{job_id}"
    reference = SurveyArtifactReference(
        survey_id=survey_id,
        user_id=active_user.id,
        job_id=job_id,
        survey_status="failed",
        job_status="finished",
        terminal_outcome="failed",
        storage_bucket="unexpected-bucket",
        storage_prefix=storage_prefix,
        manifest_key=f"{storage_prefix}/manifest.json",
    )
    with patch(
        "scholight.api.routes.survey.get_survey_artifact_reference",
        new_callable=AsyncMock,
        return_value=reference,
    ):
        response = await api_client.get(f"/surveys/{survey_id}/artifacts")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "survey_artifact_service_unavailable"


async def test_delete_uses_owner_scoped_transaction_and_returns_no_content(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authenticate(api_app, active_user)
    monkeypatch.setattr(settings, "survey_runtime_enabled", True)
    monkeypatch.setattr(settings, "survey_public_mode", "all")
    survey_id = uuid4()
    with patch(
        "scholight.api.routes.survey.delete_survey",
        new_callable=AsyncMock,
    ) as delete:
        response = await api_client.delete(f"/surveys/{survey_id}")

    assert response.status_code == 204
    delete.assert_awaited_once_with(survey_id=survey_id, user_id=active_user.id)
