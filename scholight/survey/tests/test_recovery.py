"""Archived Survey recovery verification contracts."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from scholight.survey.artifacts import SurveyArtifactStore
from scholight.survey.diagnostics import ARTIFACT_CONTRACTS
from scholight.survey.finalizer import finalize_survey
from scholight.survey.recovery import ArchivedSurveyRecoveryError, recover_archived_survey


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _complete_archived_run(root: Path) -> None:
    (root / "sections").mkdir()
    (root / "cards").mkdir()
    for relative_path in {path for contract in ARTIFACT_CONTRACTS for path in contract.required}:
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("[]" if target.suffix == ".json" else "observed\n", encoding="utf-8")
    (root / "00_card_plan.json").write_text(
        json.dumps(
            [
                {
                    "run_dir": ".",
                    "id": "cs/0012009",
                    "title": "Legacy evidence",
                    "why": "Foundational evidence",
                }
            ]
        ),
        encoding="utf-8",
    )
    (root / "00_sections.json").write_text(
        json.dumps(
            [
                {
                    "run_dir": ".",
                    "n": "01",
                    "slug": "introduction",
                    "title": "Introduction",
                    "thesis": "Establish the evidence.",
                    "card_ids": ["cs/0012009"],
                    "transfer_angle": "",
                }
            ]
        ),
        encoding="utf-8",
    )
    (root / "00_outline.md").write_text(
        "# Outline\n\n# Title\n\nRecovered Survey\n\n# Abstract\n\nVerified archived evidence.\n",
        encoding="utf-8",
    )
    (root / "sections" / "01_introduction.md").write_text(
        "## Introduction\n\nModern [2501.12345] and legacy [cs/0012009] evidence.\n",
        encoding="utf-8",
    )
    (root / "cards" / "2501.12345.md").write_text(
        "# Card\n\n- title: Modern Evidence\n", encoding="utf-8"
    )
    (root / "cards" / "cs-0012009.md").write_text(
        "# Card\n\n- title: Legacy Evidence\n", encoding="utf-8"
    )
    for judge in (
        "06a_coverage_judge.md",
        "06b_scope_judge.md",
        "06c_benchmark_judge.md",
        "06d_gap_judge.md",
    ):
        (root / judge).write_text("verdict: acceptable\n", encoding="utf-8")
    (root / "06_judge_panel.md").write_text("overall_verdict: acceptable\n", encoding="utf-8")
    finalize_survey(root)


class _Pool:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row

    async def fetchrow(self, query: str, job_id: UUID) -> dict[str, object]:
        del query, job_id
        return self.row


class _Store:
    def __init__(self, source: Path, manifest: dict[str, Any]) -> None:
        self.source = source
        self.manifest = manifest

    async def read_manifest(self, *, manifest_key: str) -> dict[str, Any]:
        del manifest_key
        return self.manifest

    async def restore_contract_workspace(
        self, *, manifest_key: str, run_root: Path
    ) -> dict[str, str]:
        del manifest_key
        shutil.copytree(self.source, run_root)
        return {
            path.relative_to(run_root).as_posix(): _digest(path)
            for path in run_root.rglob("*")
            if path.is_file()
        }


def _fixture(tmp_path: Path) -> tuple[UUID, dict[str, object], _Store, str]:
    job_id = uuid4()
    survey_id = uuid4()
    source = tmp_path / "archive"
    source.mkdir()
    _complete_archived_run(source)
    prefix = SurveyArtifactStore.prefix(user_id=42, job_id=job_id)
    files = [
        {
            "path": f"run/{path.relative_to(source).as_posix()}",
            "sha256": _digest(path),
        }
        for path in source.rglob("*")
        if path.is_file()
    ]
    manifest = {"schema_version": 1, "job_id": str(job_id), "user_id": 42, "files": files}
    row: dict[str, object] = {
        "job_id": job_id,
        "survey_id": survey_id,
        "user_id": 42,
        "job_status": "finished",
        "terminal_outcome": "failed",
        "job_error_code": "survey_contract_violation",
        "storage_bucket": "survey-test",
        "storage_prefix": prefix,
        "manifest_key": f"{prefix}/manifest.json",
        "survey_status": "failed",
        "quota_state": "released",
        "survey_error_code": "survey_contract_violation",
    }
    return job_id, row, _Store(source, manifest), _digest(source / "08_survey.md")


@pytest.mark.asyncio
async def test_dry_run_verifies_legacy_ids_without_mutating_database(tmp_path: Path) -> None:
    job_id, row, store, report_sha = _fixture(tmp_path)
    with (
        patch("scholight.survey.recovery.get_pool", return_value=_Pool(row)),
        patch(
            "scholight.survey.recovery.recover_archived_survey_contract_failure",
            new_callable=AsyncMock,
        ) as apply_recovery,
    ):
        result = await recover_archived_survey(
            job_id=job_id,
            expected_report_sha256=report_sha,
            artifact_store=store,  # type: ignore[arg-type]
        )

    assert result.report_sha256 == report_sha
    assert not result.applied
    assert not result.changed
    apply_recovery.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_requires_and_forwards_the_verified_report_guard(tmp_path: Path) -> None:
    job_id, row, store, report_sha = _fixture(tmp_path)
    with pytest.raises(ArchivedSurveyRecoveryError, match="requires"):
        await recover_archived_survey(job_id=job_id, apply=True, artifact_store=store)  # type: ignore[arg-type]

    with (
        patch("scholight.survey.recovery.get_pool", return_value=_Pool(row)),
        patch(
            "scholight.survey.recovery.recover_archived_survey_contract_failure",
            new_callable=AsyncMock,
            return_value=True,
        ) as apply_recovery,
    ):
        result = await recover_archived_survey(
            job_id=job_id,
            apply=True,
            expected_report_sha256=report_sha,
            artifact_store=store,  # type: ignore[arg-type]
        )

    assert result.applied and result.changed
    apply_recovery.assert_awaited_once_with(
        job_id=job_id,
        expected_manifest_key=row["manifest_key"],
    )
