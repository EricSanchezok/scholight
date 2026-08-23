from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from scholight.survey import production_ops
from scholight.survey.production_ops import (
    ProductionSurveyAcceptanceError,
    acceptance_payload,
    archived_evidence_repair_operation,
    rerun_identifiers,
)
from scholight.survey.quality_repair import ArchivedEvidenceRepair


def test_rerun_identifiers_are_deterministic_and_distinct() -> None:
    operation_id = UUID("b990d8c0-6367-4a4b-872a-2ca4da289365")

    first = rerun_identifiers(operation_id)
    second = rerun_identifiers(operation_id)

    assert first == second
    assert len({first.survey_id, first.draft_id, first.job_id, first.start_request_id}) == 4


@pytest.mark.asyncio
async def test_archived_evidence_verify_is_read_only_and_hash_guarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = UUID("f4795522-28f6-4edd-8813-102f654d4367")
    survey_id = UUID("c9aa7d4c-774f-4b34-9c4a-d8cc578a89e3")
    inspection = ArchivedEvidenceRepair(
        job_id=job_id,
        survey_id=survey_id,
        user_id=42,
        source_manifest_key=f"surveys/v1/42/{job_id}/manifest.json",
        source_manifest_sha256="1" * 64,
        report_sha256="2" * 64,
        manifest_key=f"surveys/v1/42/{job_id}/manifest.json",
        manifest_sha256="1" * 64,
        invalid_cards=("cards/2601.21473.md",),
        coverage_percent=99.0,
        notification_count=1,
        notification_status="succeeded",
        applied=False,
        changed=False,
    )
    create_pool = AsyncMock()
    close_pool = AsyncMock()
    inspect = AsyncMock(return_value=inspection)
    apply = AsyncMock()
    monkeypatch.setattr(production_ops, "create_pool", create_pool)
    monkeypatch.setattr(production_ops, "close_pool", close_pool)
    monkeypatch.setattr(production_ops, "inspect_archived_evidence_repair", inspect)
    monkeypatch.setattr(production_ops, "apply_archived_evidence_repair", apply)

    payload = await archived_evidence_repair_operation(
        job_id=job_id,
        apply=False,
        expected_source_manifest_sha256="1" * 64,
        expected_report_sha256="2" * 64,
    )

    assert payload["status"] == "eligible"
    assert payload["quota_state"] == "released"
    assert payload["invalid_card_count"] == 1
    assert "invalid_cards" not in payload
    apply.assert_not_awaited()
    create_pool.assert_awaited_once()
    close_pool.assert_awaited_once()


@pytest.mark.asyncio
async def test_archived_evidence_apply_reverifies_clean_released_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = UUID("f4795522-28f6-4edd-8813-102f654d4367")
    survey_id = UUID("c9aa7d4c-774f-4b34-9c4a-d8cc578a89e3")
    source_key = f"surveys/v1/42/{job_id}/manifest.json"
    overlay_key = f"surveys/v1/42/{job_id}/recoveries/{'3' * 64}/manifest.json"
    before = ArchivedEvidenceRepair(
        job_id=job_id,
        survey_id=survey_id,
        user_id=42,
        source_manifest_key=source_key,
        source_manifest_sha256="1" * 64,
        report_sha256="2" * 64,
        manifest_key=overlay_key,
        manifest_sha256="3" * 64,
        invalid_cards=("cards/2601.21473.md",),
        coverage_percent=99.0,
        notification_count=1,
        notification_status="succeeded",
        applied=True,
        changed=True,
    )
    after = ArchivedEvidenceRepair(
        job_id=job_id,
        survey_id=survey_id,
        user_id=42,
        source_manifest_key=source_key,
        source_manifest_sha256="1" * 64,
        report_sha256="2" * 64,
        manifest_key=overlay_key,
        manifest_sha256="4" * 64,
        invalid_cards=(),
        coverage_percent=100.0,
        notification_count=1,
        notification_status="succeeded",
        applied=False,
        changed=False,
    )
    monkeypatch.setattr(production_ops, "create_pool", AsyncMock())
    monkeypatch.setattr(production_ops, "close_pool", AsyncMock())
    monkeypatch.setattr(
        production_ops,
        "apply_archived_evidence_repair",
        AsyncMock(return_value=before),
    )
    inspect = AsyncMock(return_value=after)
    monkeypatch.setattr(production_ops, "inspect_archived_evidence_repair", inspect)

    payload = await archived_evidence_repair_operation(
        job_id=job_id,
        apply=True,
        expected_source_manifest_sha256="1" * 64,
        expected_report_sha256="2" * 64,
    )

    assert payload["status"] == "repaired"
    assert payload["manifest_key"] == overlay_key
    assert payload["invalid_card_count"] == 0
    assert payload["notification_count"] == 1
    assert payload["changed"] is True
    inspect.assert_awaited_once_with(job_id=job_id)


@pytest.mark.asyncio
async def test_create_rerun_preserves_source_owner_and_is_idempotently_addressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = UUID("7f0f7e61-994f-4b01-a180-3f46e54d9c0d")
    operation_id = UUID("b990d8c0-6367-4a4b-872a-2ca4da289365")
    identifiers = rerun_identifiers(operation_id)
    calls: dict[str, object] = {}

    async def source_survey(_source_id: UUID) -> tuple[int, str]:
        assert _source_id == source_id
        return 42, "Original request"

    async def create_survey(**kwargs: object) -> SimpleNamespace:
        calls["create"] = kwargs
        return SimpleNamespace(id=identifiers.survey_id, user_id=42)

    async def start_survey(**kwargs: object) -> SimpleNamespace:
        calls["start"] = kwargs
        return SimpleNamespace(id=identifiers.survey_id, status="queued")

    class Pool:
        async def fetchrow(self, _query: str, *_args: object) -> dict[str, object]:
            return {"status": "ready", "error_code": None}

    monkeypatch.setattr(production_ops, "_source_survey", source_survey)
    monkeypatch.setattr(production_ops, "create_survey", create_survey)
    monkeypatch.setattr(production_ops, "start_survey", start_survey)
    monkeypatch.setattr(production_ops, "get_pool", lambda: Pool())

    result = await production_ops._create_rerun(
        source_survey_id=source_id,
        operation_id=operation_id,
        poll_seconds=1,
        deadline=time.monotonic() + 60,
    )

    assert result == identifiers
    create_args = calls["create"]
    start_args = calls["start"]
    assert isinstance(create_args, dict)
    assert isinstance(start_args, dict)
    assert create_args["user_id"] == 42
    assert create_args["initial_request"] == "Original request"
    assert create_args["client_request_id"] == operation_id
    assert start_args["notify_on_completion"] is True
    assert start_args["job_id"] == identifiers.job_id


def test_acceptance_payload_requires_terminal_success() -> None:
    with pytest.raises(ProductionSurveyAcceptanceError, match="did not succeed"):
        acceptance_payload(
            survey_id=UUID("4ee54f64-89a1-47d5-b657-117366e3035c"),
            job_id=UUID("decc86b6-6e4d-4ac1-be86-dd127382d5c7"),
            survey_status="failed",
            job_status="finished",
            terminal_outcome="failed",
            error_code="survey_model_provider_unavailable",
            manifest_sha256="0" * 64,
            report_sha256="1" * 64,
            package_sha256="2" * 64,
            card_count=100,
            section_count=12,
            coverage_percent=100.0,
            notification_count=1,
            notification_status="succeeded",
        )


def test_acceptance_payload_rejects_degraded_success() -> None:
    with pytest.raises(ProductionSurveyAcceptanceError, match="quality checks"):
        acceptance_payload(
            survey_id=UUID("4ee54f64-89a1-47d5-b657-117366e3035c"),
            job_id=UUID("decc86b6-6e4d-4ac1-be86-dd127382d5c7"),
            survey_status="succeeded",
            job_status="finished",
            terminal_outcome="succeeded",
            error_code="survey_quality_degraded",
            manifest_sha256="0" * 64,
            report_sha256="1" * 64,
            package_sha256="2" * 64,
            card_count=100,
            section_count=12,
            coverage_percent=99.0,
            notification_count=1,
            notification_status="succeeded",
        )


def test_acceptance_payload_rejects_incomplete_evidence_declarations() -> None:
    with pytest.raises(ProductionSurveyAcceptanceError, match="declarations"):
        acceptance_payload(
            survey_id=UUID("4ee54f64-89a1-47d5-b657-117366e3035c"),
            job_id=UUID("decc86b6-6e4d-4ac1-be86-dd127382d5c7"),
            survey_status="succeeded",
            job_status="finished",
            terminal_outcome="succeeded",
            error_code=None,
            manifest_sha256="0" * 64,
            report_sha256="1" * 64,
            package_sha256="2" * 64,
            card_count=100,
            section_count=12,
            coverage_percent=99.0,
            notification_count=1,
            notification_status="succeeded",
            unknown_count=1,
        )


def test_acceptance_payload_requires_consumed_quota_for_new_reruns() -> None:
    with pytest.raises(ProductionSurveyAcceptanceError, match="allowance"):
        acceptance_payload(
            survey_id=UUID("4ee54f64-89a1-47d5-b657-117366e3035c"),
            job_id=UUID("decc86b6-6e4d-4ac1-be86-dd127382d5c7"),
            survey_status="succeeded",
            job_status="finished",
            terminal_outcome="succeeded",
            error_code=None,
            manifest_sha256="0" * 64,
            report_sha256="1" * 64,
            package_sha256="2" * 64,
            card_count=100,
            section_count=12,
            coverage_percent=99.0,
            notification_count=1,
            notification_status="succeeded",
            quota_state="released",
        )


def test_acceptance_payload_requires_eighty_percent_full_text_coverage() -> None:
    with pytest.raises(ProductionSurveyAcceptanceError, match="below 80"):
        acceptance_payload(
            survey_id=UUID("4ee54f64-89a1-47d5-b657-117366e3035c"),
            job_id=UUID("decc86b6-6e4d-4ac1-be86-dd127382d5c7"),
            survey_status="succeeded",
            job_status="finished",
            terminal_outcome="succeeded",
            error_code=None,
            manifest_sha256="0" * 64,
            report_sha256="1" * 64,
            package_sha256="2" * 64,
            card_count=100,
            section_count=12,
            coverage_percent=79.99,
            notification_count=1,
            notification_status="succeeded",
        )


def test_acceptance_payload_requires_one_successful_notification() -> None:
    with pytest.raises(ProductionSurveyAcceptanceError, match="notification"):
        acceptance_payload(
            survey_id=UUID("4ee54f64-89a1-47d5-b657-117366e3035c"),
            job_id=UUID("decc86b6-6e4d-4ac1-be86-dd127382d5c7"),
            survey_status="succeeded",
            job_status="finished",
            terminal_outcome="succeeded",
            error_code=None,
            manifest_sha256="0" * 64,
            report_sha256="1" * 64,
            package_sha256="2" * 64,
            card_count=100,
            section_count=12,
            coverage_percent=100.0,
            notification_count=2,
            notification_status="succeeded",
        )


def test_acceptance_payload_returns_only_bounded_verification_fields() -> None:
    payload = acceptance_payload(
        survey_id=UUID("4ee54f64-89a1-47d5-b657-117366e3035c"),
        job_id=UUID("decc86b6-6e4d-4ac1-be86-dd127382d5c7"),
        survey_status="succeeded",
        job_status="finished",
        terminal_outcome="succeeded",
        error_code=None,
        manifest_sha256="0" * 64,
        report_sha256="1" * 64,
        package_sha256="2" * 64,
        card_count=100,
        section_count=12,
        coverage_percent=98.0,
        notification_count=1,
        notification_status="succeeded",
    )

    assert payload == {
        "survey_id": "4ee54f64-89a1-47d5-b657-117366e3035c",
        "job_id": "decc86b6-6e4d-4ac1-be86-dd127382d5c7",
        "status": "succeeded",
        "manifest_sha256": "0" * 64,
        "report_sha256": "1" * 64,
        "package_sha256": "2" * 64,
        "card_count": 100,
        "section_count": 12,
        "coverage_percent": 98.0,
        "notification_count": 1,
        "notification_status": "succeeded",
    }
