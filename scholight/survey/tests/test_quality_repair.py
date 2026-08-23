"""Archived degraded Survey evidence repair contracts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from scholight.config import settings
from scholight.survey.artifacts import SurveyRecoveryOverlay
from scholight.survey.evidence import SurveyEvidenceSummary
from scholight.survey.quality_repair import (
    ArchivedEvidenceRepair,
    ArchivedEvidenceRepairError,
    apply_archived_evidence_repair,
    inspect_archived_evidence_repair,
)


@pytest.mark.asyncio
async def test_apply_rejects_changed_hash_before_model_or_object_write() -> None:
    job_id = uuid4()
    inspection = ArchivedEvidenceRepair(
        job_id=job_id,
        survey_id=uuid4(),
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

    with (
        patch(
            "scholight.survey.quality_repair.inspect_archived_evidence_repair",
            new_callable=AsyncMock,
            return_value=inspection,
        ),
        patch(
            "scholight.survey.quality_repair._run_repair_workflow",
            new_callable=AsyncMock,
        ) as run_repair,
        pytest.raises(ArchivedEvidenceRepairError, match="checksum guard changed"),
    ):
        await apply_archived_evidence_repair(
            job_id=job_id,
            expected_source_manifest_sha256="3" * 64,
            expected_report_sha256="2" * 64,
        )

    run_repair.assert_not_awaited()


@pytest.mark.asyncio
async def test_inspection_is_read_only_and_returns_only_hash_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    survey_id = uuid4()
    prefix = f"surveys/v1/42/{job_id}"
    manifest_key = f"{prefix}/manifest.json"
    report_sha256 = "1" * 64
    manifest_sha256 = "2" * 64
    pool = AsyncMock()
    pool.fetchrow.return_value = {
        "job_id": job_id,
        "survey_id": survey_id,
        "user_id": 42,
        "survey_status": "succeeded",
        "quota_state": "released",
        "survey_error_code": "survey_quality_degraded",
        "job_status": "finished",
        "terminal_outcome": "succeeded",
        "job_error_code": "survey_quality_degraded",
        "storage_bucket": "survey-test",
        "storage_prefix": prefix,
        "manifest_key": manifest_key,
        "notification_count": 1,
        "notification_status": "succeeded",
    }
    store = AsyncMock()
    store.read_manifest_with_sha256.return_value = (
        {
            "schema_version": 1,
            "job_id": str(job_id),
            "user_id": 42,
            "files": [
                {
                    "path": "run/08_survey.md",
                    "sha256": report_sha256,
                }
            ],
        },
        manifest_sha256,
    )

    async def restore(*, manifest_key: str, run_root: Path) -> dict[str, str]:
        assert manifest_key == f"{prefix}/manifest.json"
        (run_root / "cards").mkdir(parents=True)
        (run_root / "cards" / "2601.21473.md").write_text(
            "# Card without evidence declaration\n",
            encoding="utf-8",
        )
        (run_root / "00_card_plan.json").write_text(
            json.dumps(
                [
                    {
                        "run_dir": ".",
                        "id": "2601.21473",
                        "title": "ScaleSim",
                        "why": "multi-agent simulation",
                    }
                ]
            ),
            encoding="utf-8",
        )
        (run_root / "08_survey.md").write_text("# Existing report\n", encoding="utf-8")
        (run_root / "index.md").write_text("# Existing index\n", encoding="utf-8")
        return {}

    store.restore_contract_workspace.side_effect = restore
    monkeypatch.setattr(settings, "data_root", str(tmp_path))

    with patch("scholight.survey.quality_repair.get_pool", return_value=pool):
        result = await inspect_archived_evidence_repair(job_id=job_id, artifact_store=store)

    assert result.source_manifest_sha256 == manifest_sha256
    assert result.report_sha256 == report_sha256
    assert result.invalid_cards == ("cards/2601.21473.md",)
    assert result.applied is False
    store.create_evidence_repair_overlay.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_repairs_only_selected_cards_and_activates_overlay_after_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    survey_id = uuid4()
    prefix = f"surveys/v1/42/{job_id}"
    source_manifest_key = f"{prefix}/manifest.json"
    replacement_manifest_key = f"{prefix}/recoveries/{'3' * 64}/manifest.json"
    inspection = ArchivedEvidenceRepair(
        job_id=job_id,
        survey_id=survey_id,
        user_id=42,
        source_manifest_key=source_manifest_key,
        source_manifest_sha256="1" * 64,
        report_sha256="2" * 64,
        manifest_key=source_manifest_key,
        manifest_sha256="1" * 64,
        invalid_cards=("cards/2601.21473.md",),
        coverage_percent=99.0,
        notification_count=1,
        notification_status="succeeded",
        applied=False,
        changed=False,
    )
    pool = AsyncMock()
    pool.fetchrow.return_value = {
        "job_id": job_id,
        "survey_id": survey_id,
        "user_id": 42,
        "survey_status": "succeeded",
        "quota_state": "released",
        "survey_error_code": "survey_quality_degraded",
        "job_status": "finished",
        "terminal_outcome": "succeeded",
        "job_error_code": "survey_quality_degraded",
        "storage_bucket": "survey-test",
        "storage_prefix": prefix,
        "manifest_key": source_manifest_key,
        "notification_count": 1,
        "notification_status": "succeeded",
    }
    store = AsyncMock()

    async def restore(*, manifest_key: str, run_root: Path) -> dict[str, str]:
        assert manifest_key == source_manifest_key
        (run_root / "cards").mkdir(parents=True)
        (run_root / "cards" / "2601.21473.md").write_text(
            "# Invalid card\n",
            encoding="utf-8",
        )
        (run_root / "08_survey.md").write_text("# Existing report\n", encoding="utf-8")
        (run_root / "index.md").write_text("# Existing index\n", encoding="utf-8")
        return {}

    async def repair(**kwargs: object) -> bool:
        run_root = kwargs["run_root"]
        assert isinstance(run_root, Path)
        (run_root / "cards" / "2601.21473.md").write_text(
            "# Repaired card\n\n## evidence\n- level: full_text\n- reason: pdf_text_extracted\n",
            encoding="utf-8",
        )
        return True

    store.restore_contract_workspace.side_effect = restore
    store.create_evidence_repair_overlay.return_value = SurveyRecoveryOverlay(
        source_manifest_key=source_manifest_key,
        source_manifest_sha256="1" * 64,
        storage_prefix=prefix,
        manifest_key=replacement_manifest_key,
        manifest={"schema_version": 3},
    )
    store.read_manifest_with_sha256.return_value = ({"schema_version": 3}, "4" * 64)
    clean_summary = SurveyEvidenceSummary(
        card_count=1,
        counts={
            "html": 0,
            "full_text": 1,
            "partial": 0,
            "abstract_only": 0,
            "unknown": 0,
        },
        reviewed_count=1,
        coverage_percent=100.0,
        invalid_reason_count=0,
        runtime_marker_count=0,
        invalid_cards=(),
    )
    selected = (
        {
            "run_dir": ".",
            "id": "2601.21473",
            "title": "ScaleSim",
            "why": "multi-agent simulation",
        },
    )
    monkeypatch.setattr(settings, "data_root", str(tmp_path))

    with (
        patch(
            "scholight.survey.quality_repair.inspect_archived_evidence_repair",
            new_callable=AsyncMock,
            return_value=inspection,
        ),
        patch("scholight.survey.quality_repair.get_pool", return_value=pool),
        patch(
            "scholight.survey.quality_repair._invalid_evidence_repair_items",
            return_value=selected,
        ),
        patch(
            "scholight.survey.quality_repair._run_repair_workflow",
            new_callable=AsyncMock,
            side_effect=repair,
        ) as run_repair,
        patch(
            "scholight.survey.quality_repair.audit_survey_evidence",
            return_value=clean_summary,
        ),
        patch(
            "scholight.survey.quality_repair.finalize_survey",
            side_effect=lambda run_root: SimpleNamespace(report_path=run_root / "08_survey.md"),
        ),
        patch(
            "scholight.survey.quality_repair.repair_degraded_survey_evidence",
            new_callable=AsyncMock,
            return_value=True,
        ) as activate,
    ):
        result = await apply_archived_evidence_repair(
            job_id=job_id,
            expected_source_manifest_sha256="1" * 64,
            expected_report_sha256="2" * 64,
            artifact_store=store,
        )

    assert result.manifest_key == replacement_manifest_key
    assert result.applied is True
    assert result.changed is True
    run_repair.assert_awaited_once()
    repair_call = run_repair.await_args
    assert repair_call is not None
    store.create_evidence_repair_overlay.assert_awaited_once_with(
        source_manifest_key=source_manifest_key,
        expected_source_sha256="1" * 64,
        run_root=repair_call.kwargs["run_root"],
        repaired_cards=("cards/2601.21473.md",),
    )
    activate.assert_awaited_once_with(
        job_id=job_id,
        expected_manifest_key=source_manifest_key,
        replacement_manifest_key=replacement_manifest_key,
    )
