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

from scholight.survey.artifacts import SurveyArtifactStore, SurveyRecoveryOverlay
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
    (root / "00_outline.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "title": "Recovered Survey",
                "abstract": "Verified archived evidence.",
                "through_line": "Evidence first.",
            }
        ),
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
        self.manifest_body = json.dumps(
            manifest,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        self.overlay_writes = 0

    async def read_manifest(self, *, manifest_key: str) -> dict[str, Any]:
        del manifest_key
        return self.manifest

    async def read_manifest_with_sha256(
        self,
        *,
        manifest_key: str,
    ) -> tuple[dict[str, Any], str]:
        del manifest_key
        return self.manifest, hashlib.sha256(self.manifest_body).hexdigest()

    async def validate_manifest(self, *, manifest_key: str) -> None:
        del manifest_key

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

    def _overlay(
        self,
        *,
        source_manifest_key: str,
        expected_source_sha256: str,
        report_path: Path,
        index_path: Path,
    ) -> SurveyRecoveryOverlay:
        report = report_path.read_bytes()
        index = index_path.read_bytes()
        report_sha256 = hashlib.sha256(report).hexdigest()
        prefix = source_manifest_key.removesuffix("/manifest.json")
        recovery_prefix = f"{prefix}/recoveries/{report_sha256}"
        manifest = {
            "schema_version": 2,
            "job_id": self.manifest["job_id"],
            "user_id": self.manifest["user_id"],
            "parent_manifest": {
                "key": source_manifest_key,
                "sha256": expected_source_sha256,
            },
            "files": [
                {
                    "path": path,
                    "key": f"{recovery_prefix}/{path}",
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "mime": "text/markdown",
                }
                for path, content in (
                    ("run/08_survey.md", report),
                    ("run/index.md", index),
                )
            ],
        }
        return SurveyRecoveryOverlay(
            source_manifest_key=source_manifest_key,
            source_manifest_sha256=expected_source_sha256,
            storage_prefix=prefix,
            manifest_key=f"{recovery_prefix}/manifest.json",
            manifest=manifest,
        )

    async def plan_recovery_overlay(self, **kwargs: Any) -> SurveyRecoveryOverlay:
        return self._overlay(**kwargs)

    async def create_recovery_overlay(self, **kwargs: Any) -> SurveyRecoveryOverlay:
        self.overlay_writes += 1
        return self._overlay(**kwargs)


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
    assert result.source_manifest_sha256 == hashlib.sha256(store.manifest_body).hexdigest()
    assert result.recovery_type == "exact_report_reclassification"
    assert not result.applied
    assert not result.changed
    apply_recovery.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_accepts_historical_intermediate_contracts(tmp_path: Path) -> None:
    job_id, row, store, report_sha = _fixture(tmp_path)
    legacy_artifacts = {
        "00_card_plan.json": json.dumps({"papers": ["cs/0012009"]}),
        "00_sections.json": json.dumps({"sections": ["introduction"]}),
        "06a_coverage_judge.md": "# Coverage\n\nDecision: acceptable\n",
        "06b_scope_judge.md": "# Scope\n\nDecision: acceptable\n",
        "06c_benchmark_judge.md": "# Benchmark\n\nDecision: acceptable\n",
        "06d_gap_judge.md": "# Gaps\n\nDecision: acceptable\n",
        "06_judge_panel.md": "# Panel\n\nDecision: acceptable\n",
    }
    for relative_path, content in legacy_artifacts.items():
        artifact = store.source / relative_path
        artifact.write_text(content, encoding="utf-8")
        manifest_path = f"run/{relative_path}"
        for record in store.manifest["files"]:
            if record["path"] == manifest_path:
                record["sha256"] = _digest(artifact)
                break

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
            expected_source_manifest_sha256=hashlib.sha256(store.manifest_body).hexdigest(),
            expected_report_sha256=report_sha,
            artifact_store=store,  # type: ignore[arg-type]
        )

    assert result.applied and result.changed
    apply_recovery.assert_awaited_once_with(
        job_id=job_id,
        expected_manifest_key=row["manifest_key"],
        expected_error_code="survey_contract_violation",
        replacement_manifest_key=None,
    )


@pytest.mark.asyncio
async def test_report_missing_dry_run_plans_v2_without_writes(tmp_path: Path) -> None:
    job_id, row, store, _report_sha = _fixture(tmp_path)
    row["job_error_code"] = "survey_report_missing"
    row["survey_error_code"] = "survey_report_missing"
    for name in ("08_survey.md", "index.md"):
        (store.source / name).unlink()
        store.manifest["files"] = [
            record for record in store.manifest["files"] if record["path"] != f"run/{name}"
        ]
    store.manifest_body = json.dumps(
        store.manifest,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()

    with (
        patch("scholight.survey.recovery.get_pool", return_value=_Pool(row)),
        patch(
            "scholight.survey.recovery.recover_archived_survey_contract_failure",
            new_callable=AsyncMock,
        ) as apply_recovery,
    ):
        result = await recover_archived_survey(
            job_id=job_id,
            artifact_store=store,  # type: ignore[arg-type]
        )

    assert result.recovery_type == "deterministic_finalization"
    assert result.expected_manifest["schema_version"] == 2
    assert result.manifest_key.endswith(f"/{result.report_sha256}/manifest.json")
    assert store.overlay_writes == 0
    apply_recovery.assert_not_awaited()


@pytest.mark.asyncio
async def test_report_missing_accepts_complete_legacy_card_plan(tmp_path: Path) -> None:
    job_id, row, store, _report_sha = _fixture(tmp_path)
    row["job_error_code"] = "survey_report_missing"
    row["survey_error_code"] = "survey_report_missing"
    for name in ("08_survey.md", "index.md"):
        (store.source / name).unlink()
        store.manifest["files"] = [
            record for record in store.manifest["files"] if record["path"] != f"run/{name}"
        ]

    plan = [
        {
            "run_dir": ".",
            "id": f"2501.{index:05d}",
            "title": f"Archived paper {index}",
            "why": "Archived evidence",
        }
        for index in range(135)
    ]
    plan_path = store.source / "00_card_plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    for record in store.manifest["files"]:
        if record["path"] == "run/00_card_plan.json":
            record["sha256"] = _digest(plan_path)
            break
    for item in plan:
        card_path = store.source / "cards" / f"{item['id']}.md"
        card_path.write_text(f"# Card\n\n- title: {item['title']}\n", encoding="utf-8")
        store.manifest["files"].append(
            {"path": f"run/cards/{card_path.name}", "sha256": _digest(card_path)}
        )
    store.manifest_body = json.dumps(
        store.manifest,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()

    with patch("scholight.survey.recovery.get_pool", return_value=_Pool(row)):
        result = await recover_archived_survey(
            job_id=job_id,
            artifact_store=store,  # type: ignore[arg-type]
        )

    assert result.recovery_type == "deterministic_finalization"
    assert result.expected_manifest["schema_version"] == 2


@pytest.mark.asyncio
async def test_report_missing_apply_requires_both_hashes_and_switches_to_v2(
    tmp_path: Path,
) -> None:
    job_id, row, store, _report_sha = _fixture(tmp_path)
    row["job_error_code"] = "survey_report_missing"
    row["survey_error_code"] = "survey_report_missing"
    for name in ("08_survey.md", "index.md"):
        (store.source / name).unlink()
        store.manifest["files"] = [
            record for record in store.manifest["files"] if record["path"] != f"run/{name}"
        ]
    store.manifest_body = json.dumps(
        store.manifest,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    source_sha256 = hashlib.sha256(store.manifest_body).hexdigest()
    with patch("scholight.survey.recovery.get_pool", return_value=_Pool(row)):
        dry_run = await recover_archived_survey(
            job_id=job_id,
            artifact_store=store,  # type: ignore[arg-type]
        )

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
            expected_source_manifest_sha256=source_sha256,
            expected_report_sha256=dry_run.report_sha256,
            artifact_store=store,  # type: ignore[arg-type]
        )

    assert result.changed
    assert store.overlay_writes == 1
    apply_recovery.assert_awaited_once_with(
        job_id=job_id,
        expected_manifest_key=row["manifest_key"],
        expected_error_code="survey_report_missing",
        replacement_manifest_key=result.manifest_key,
    )


@pytest.mark.asyncio
async def test_report_missing_rejects_incomplete_plans(tmp_path: Path) -> None:
    job_id, row, store, _report_sha = _fixture(tmp_path)
    row["job_error_code"] = "survey_report_missing"
    row["survey_error_code"] = "survey_report_missing"
    (store.source / "sections" / "01_introduction.md").unlink()

    with (
        patch("scholight.survey.recovery.get_pool", return_value=_Pool(row)),
        pytest.raises(ArchivedSurveyRecoveryError, match="complete validated plans"),
    ):
        await recover_archived_survey(
            job_id=job_id,
            artifact_store=store,  # type: ignore[arg-type]
        )

    assert store.overlay_writes == 0


@pytest.mark.asyncio
async def test_report_guard_mismatch_does_not_write_an_overlay(tmp_path: Path) -> None:
    job_id, row, store, _report_sha = _fixture(tmp_path)
    row["job_error_code"] = "survey_report_missing"
    row["survey_error_code"] = "survey_report_missing"
    for name in ("08_survey.md", "index.md"):
        (store.source / name).unlink()
        store.manifest["files"] = [
            record for record in store.manifest["files"] if record["path"] != f"run/{name}"
        ]
    store.manifest_body = json.dumps(
        store.manifest,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()

    with (
        patch("scholight.survey.recovery.get_pool", return_value=_Pool(row)),
        patch(
            "scholight.survey.recovery.recover_archived_survey_contract_failure",
            new_callable=AsyncMock,
        ) as apply_recovery,
        pytest.raises(ArchivedSurveyRecoveryError, match="report SHA256"),
    ):
        await recover_archived_survey(
            job_id=job_id,
            apply=True,
            expected_source_manifest_sha256=hashlib.sha256(store.manifest_body).hexdigest(),
            expected_report_sha256="0" * 64,
            artifact_store=store,  # type: ignore[arg-type]
        )

    assert store.overlay_writes == 0
    apply_recovery.assert_not_awaited()
