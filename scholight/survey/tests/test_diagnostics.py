"""Runtime-owned Survey diagnostic trace and artifact contract tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from scholight.survey.diagnostics import (
    ARTIFACT_CONTRACTS,
    SurveyDiagnostics,
    sanitize_diagnostic_value,
    sanitize_tool_arguments,
)


def test_sanitizer_redacts_secrets_and_normalizes_workspace(tmp_path: Path) -> None:
    value = {
        "authorization": "Bearer secret-token",
        "query": "reasoning compression",
        "path": str(tmp_path / "cards" / "paper.md"),
        "nested": {"api_key": "sk_live_private"},
    }

    sanitized = sanitize_diagnostic_value(value, run_root=tmp_path)

    assert sanitized == {
        "authorization": "<redacted>",
        "query": "reasoning compression",
        "path": "<run_dir>/cards/paper.md",
        "nested": {"api_key": "<redacted>"},
    }


def test_trace_persists_events_and_crash_safe_snapshot(tmp_path: Path) -> None:
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )

    diagnostics.record("run.started", component="survey_pipeline")
    diagnostics.record(
        "tool.finished",
        component="method_scout",
        tool="scholight__search_papers",
        status="succeeded",
        arguments={"query": "chain of thought compression", "token": "secret"},
    )

    events = [
        json.loads(line)
        for line in (tmp_path / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    snapshot = json.loads((tmp_path / "diagnostics.json").read_text(encoding="utf-8"))

    assert [event["type"] for event in events] == ["run.started", "tool.finished"]
    assert events[1]["arguments"]["token"] == "<redacted>"
    assert snapshot["schema_version"] == 1
    assert snapshot["event_count"] == 2
    assert snapshot["tool_counts"] == {"failed": 0, "finished": 1, "started": 0}
    last_activity = datetime.fromisoformat(snapshot["last_activity_at"])
    assert diagnostics.last_activity_age_seconds(now=last_activity + timedelta(seconds=75)) == 75


def test_completed_component_records_missing_primary_artifact(tmp_path: Path) -> None:
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )

    diagnostics.component_finished("discovery_merger", status="completed")

    snapshot = diagnostics.snapshot()
    assert snapshot["first_anomaly"] == {
        "component": "discovery_merger",
        "expected_artifact": "02_candidate_pool.md",
        "kind": "required_artifact_missing",
        "severity": "error",
    }
    assert snapshot["affected_components"] == [
        "expansion",
        "rank_pool",
        "card_plan",
        "research_map",
        "judge_panel",
        "image_planner",
        "survey_outline",
        "survey_finalizer",
    ]


def test_contract_observation_never_stops_later_components(tmp_path: Path) -> None:
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )

    diagnostics.component_finished("discovery_merger", status="completed")
    (tmp_path / "03a_seed_papers.md").write_text("# Seeds", encoding="utf-8")
    diagnostics.component_finished("citation_seed_selector", status="completed")

    snapshot = diagnostics.snapshot()
    assert snapshot["last_successful_component"] == "citation_seed_selector"
    assert snapshot["anomaly_count"] == 1


def test_final_audit_drops_resolved_component_missing_anomaly(tmp_path: Path) -> None:
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )
    diagnostics.component_finished("gap_judge", status="completed")
    (tmp_path / "06d_gap_judge.md").write_text("verdict: acceptable\n", encoding="utf-8")

    diagnostics.finalize_contract_audit()

    assert not any(
        anomaly["component"] == "gap_judge"
        and anomaly["expected_artifact"] == "06d_gap_judge.md"
        and anomaly["kind"] == "required_artifact_missing"
        for anomaly in diagnostics.snapshot()["anomalies"]
    )


def test_optional_image_is_a_warning_only_at_final_audit(tmp_path: Path) -> None:
    (tmp_path / "08_survey.md").write_text("# Survey", encoding="utf-8")
    (tmp_path / "index.md").write_text("# Survey", encoding="utf-8")
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )

    diagnostics.finalize_contract_audit()

    anomalies = diagnostics.snapshot()["anomalies"]
    assert {
        "component": "image_planner",
        "expected_artifact": "08_global_picture.png",
        "kind": "optional_artifact_missing",
        "severity": "warning",
    } in anomalies


def test_final_audit_accepts_matching_unnumbered_section_headings(tmp_path: Path) -> None:
    sections = tmp_path / "sections"
    sections.mkdir()
    (sections / "01_introduction.md").write_text(
        "## Introduction: The Reliability Problem\n\nBody.",
        encoding="utf-8",
    )
    (sections / "02_research_arc.md").write_text(
        "## Research Arc — From Heuristics to Evaluation\n\nBody.",
        encoding="utf-8",
    )
    (tmp_path / "08_survey.md").write_text(
        "# Survey\n\n"
        "## Introduction: The Reliability Problem\n\nBody.\n\n"
        "## Research Arc — From Heuristics to Evaluation\n\nBody.\n\n"
        "## References\n\n1. Paper.\n",
        encoding="utf-8",
    )
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )

    diagnostics.finalize_contract_audit()

    assert not any(
        anomaly["kind"] == "section_missing_from_final_report"
        for anomaly in diagnostics.snapshot()["anomalies"]
    )


def test_final_audit_rejects_changed_unnumbered_section_heading(tmp_path: Path) -> None:
    sections = tmp_path / "sections"
    sections.mkdir()
    (sections / "01_introduction.md").write_text(
        "## Introduction: The Reliability Problem\n\nBody.",
        encoding="utf-8",
    )
    (tmp_path / "08_survey.md").write_text(
        "# Survey\n\n## A Different Introduction\n\nBody.\n\n## References\n\n1. Paper.\n",
        encoding="utf-8",
    )
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )

    diagnostics.finalize_contract_audit()

    assert {
        "component": "survey_finalizer",
        "expected_artifact": "08_survey.md#section-01",
        "kind": "section_missing_from_final_report",
        "severity": "error",
    } in diagnostics.snapshot()["anomalies"]


def test_final_audit_infers_last_component_from_artifacts_when_events_are_missing(
    tmp_path: Path,
) -> None:
    for name in (
        "00_survey_spec.md",
        "01_query_plan.md",
        "02_candidate_pool.md",
        "03_expansion.md",
        "04_ranked_pool.md",
        "05_research_map.md",
        "06_judge_panel.md",
        "00_outline.json",
        "00_outline.md",
        "00_card_plan.json",
        "00_sections.json",
    ):
        content = "[]" if name.endswith(".json") else "observed"
        if name == "00_outline.json":
            content = (
                '{"schema_version":1,"title":"Observed","abstract":"Observed",'
                '"through_line":"Observed"}'
            )
        (tmp_path / name).write_text(content, encoding="utf-8")
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )
    diagnostics.component_finished("discovery_merger", status="completed")

    diagnostics.finalize_contract_audit()

    assert diagnostics.snapshot()["last_successful_component"] == "survey_outline"


def test_required_anomaly_precedes_optional_warning(tmp_path: Path) -> None:
    required = {
        path
        for contract in ARTIFACT_CONTRACTS
        for path in contract.required
        if path != "08_survey.md"
    }
    for relative_path in required:
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "[]" if path.suffix == ".json" else "observed"
        path.write_text(content, encoding="utf-8")
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )

    diagnostics.finalize_contract_audit()

    assert diagnostics.snapshot()["first_anomaly"] == {
        "component": "survey_finalizer",
        "expected_artifact": "08_survey.md",
        "kind": "required_artifact_missing",
        "severity": "error",
    }


def test_diagnostic_io_failure_is_best_effort(tmp_path: Path) -> None:
    run_root = tmp_path / "missing" / "run"
    diagnostics = SurveyDiagnostics(
        run_root=run_root,
        job_id=uuid4(),
        survey_id=uuid4(),
    )

    diagnostics.record("run.started")

    assert diagnostics.write_failure_count == 1


def test_spawn_arguments_keep_ids_but_drop_instructions_and_prose(tmp_path: Path) -> None:
    sanitized = sanitize_tool_arguments(
        "spawn_PaperCard",
        {
            "items": [
                {
                    "id": "2501.12345",
                    "title": "Private title",
                    "why": "Long model-authored rationale",
                    "instruction": "Hidden worker instruction",
                }
            ],
            "max_parallel": 20,
        },
        run_root=tmp_path,
    )

    assert sanitized == {
        "item_count": 1,
        "item_ids": ["2501.12345"],
        "max_parallel": 20,
    }


def test_spawned_outputs_are_checked_during_final_audit(tmp_path: Path) -> None:
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )
    diagnostics.tool_event(
        tool="spawn_PaperCard",
        status="started",
        component="card_plan",
        arguments={"items": [{"id": "2501.12345"}]},
    )
    diagnostics.tool_event(
        tool="spawn_SectionExpander",
        status="started",
        component="survey_outline",
        arguments={"items": [{"n": "01", "slug": "introduction"}]},
    )

    diagnostics.finalize_contract_audit()

    missing = {anomaly["expected_artifact"] for anomaly in diagnostics.snapshot()["anomalies"]}
    assert "cards/2501.12345.md" in missing
    assert "sections/01_introduction.md" in missing


def test_legacy_arxiv_ids_use_safe_card_artifact_names(tmp_path: Path) -> None:
    (tmp_path / "00_card_plan.json").write_text(
        json.dumps(
            [
                {
                    "run_dir": ".",
                    "id": "cs/0012009",
                    "title": "Legacy paper",
                    "why": "Foundational evidence",
                }
            ]
        ),
        encoding="utf-8",
    )
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )

    diagnostics.observe_artifacts()

    assert diagnostics.snapshot()["expected_dynamic_artifacts"] == ["cards/cs-0012009.md"]


@pytest.mark.parametrize("unsafe_id", ("../paper", "/paper", "cs\\0012009", "bad/1234"))
def test_card_plan_rejects_unsafe_or_noncanonical_ids(tmp_path: Path, unsafe_id: str) -> None:
    (tmp_path / "00_card_plan.json").write_text(
        json.dumps(
            [
                {
                    "run_dir": ".",
                    "id": unsafe_id,
                    "title": "Unsafe paper",
                    "why": "Must be rejected",
                }
            ]
        ),
        encoding="utf-8",
    )
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )

    diagnostics.finalize_contract_audit()

    assert any(
        anomaly["kind"] == "plan_artifact_invalid"
        for anomaly in diagnostics.snapshot()["anomalies"]
    )


def test_final_audit_rejects_missing_or_invalid_judge_verdicts(tmp_path: Path) -> None:
    judge_files = {
        "06a_coverage_judge.md": "verdict: acceptable\n",
        "06b_scope_judge.md": "no verdict here\n",
        "06c_benchmark_judge.md": "verdict: excellent\n",
        "06d_gap_judge.md": "verdict: strong\nverdict: blocked\n",
        "06_judge_panel.md": "overall_verdict: acceptable\n",
    }
    for name, content in judge_files.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )

    diagnostics.finalize_contract_audit()

    verdict_errors = {
        anomaly["expected_artifact"]
        for anomaly in diagnostics.snapshot()["anomalies"]
        if anomaly["kind"] == "judge_verdict_invalid"
    }
    assert verdict_errors == {
        "06b_scope_judge.md#verdict",
        "06c_benchmark_judge.md#verdict",
        "06d_gap_judge.md#verdict",
    }


def test_final_audit_accepts_exact_judge_verdict_contract(tmp_path: Path) -> None:
    for name in (
        "06a_coverage_judge.md",
        "06b_scope_judge.md",
        "06c_benchmark_judge.md",
        "06d_gap_judge.md",
    ):
        (tmp_path / name).write_text("verdict: acceptable\n", encoding="utf-8")
    (tmp_path / "06_judge_panel.md").write_text("overall_verdict: strong\n", encoding="utf-8")
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )

    diagnostics.finalize_contract_audit()

    assert not any(
        anomaly["kind"] == "judge_verdict_invalid"
        for anomaly in diagnostics.snapshot()["anomalies"]
    )


def test_all_spawned_card_outputs_are_tracked_beyond_log_preview_limit(tmp_path: Path) -> None:
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )

    diagnostics.tool_event(
        tool="spawn_PaperCard",
        status="started",
        component="card_plan",
        arguments={"items": [{"id": f"2501.{index:05d}"} for index in range(100)]},
    )

    assert len(diagnostics.snapshot()["expected_dynamic_artifacts"]) == 100


def test_durable_plans_restore_spawn_expectations_without_runtime_events(tmp_path: Path) -> None:
    (tmp_path / "00_card_plan.json").write_text(
        json.dumps(
            [
                {
                    "run_dir": ".",
                    "id": "2501.12345",
                    "title": "Paper",
                    "why": "Core evidence",
                }
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "00_sections.json").write_text(
        json.dumps(
            [
                {
                    "run_dir": ".",
                    "n": "01",
                    "slug": "introduction",
                    "title": "Introduction",
                    "thesis": "Establish the problem.",
                    "card_ids": ["2501.12345"],
                    "transfer_angle": "",
                }
            ]
        ),
        encoding="utf-8",
    )
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )

    diagnostics.observe_artifacts()

    assert diagnostics.snapshot()["expected_dynamic_artifacts"] == [
        "cards/2501.12345.md",
        "sections/01_introduction.md",
    ]


def test_durable_plan_accepts_the_exact_absolute_run_directory(tmp_path: Path) -> None:
    (tmp_path / "00_card_plan.json").write_text(
        json.dumps(
            [
                {
                    "run_dir": str(tmp_path),
                    "id": "2501.12345",
                    "title": "Paper",
                    "why": "Core evidence",
                }
            ]
        ),
        encoding="utf-8",
    )
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )

    diagnostics.observe_artifacts()

    assert diagnostics.snapshot()["expected_dynamic_artifacts"] == ["cards/2501.12345.md"]


def test_section_plan_normalizes_unique_legacy_card_artifact_stem(tmp_path: Path) -> None:
    (tmp_path / "00_card_plan.json").write_text(
        json.dumps(
            [
                {
                    "run_dir": ".",
                    "id": "math/0208020",
                    "title": "Legacy paper",
                    "why": "Foundational evidence",
                }
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "00_sections.json").write_text(
        json.dumps(
            [
                {
                    "run_dir": ".",
                    "n": "01",
                    "slug": "introduction",
                    "title": "Introduction",
                    "thesis": "Establish the problem.",
                    "card_ids": ["math-0208020"],
                    "transfer_angle": "",
                }
            ]
        ),
        encoding="utf-8",
    )
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )

    plan = diagnostics.read_durable_plan("00_sections.json")

    assert plan is not None
    assert plan[0]["card_ids"] == ["math/0208020"]


def test_invalid_durable_plan_is_a_contract_error(tmp_path: Path) -> None:
    (tmp_path / "00_card_plan.json").write_text("not json", encoding="utf-8")
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )

    diagnostics.finalize_contract_audit()

    assert {
        "component": "card_plan",
        "expected_artifact": "00_card_plan.json",
        "kind": "plan_artifact_invalid",
        "severity": "error",
    } in diagnostics.snapshot()["anomalies"]


def test_card_plan_over_budget_is_a_contract_error(tmp_path: Path) -> None:
    (tmp_path / "00_card_plan.json").write_text(
        json.dumps(
            [
                {
                    "run_dir": ".",
                    "id": f"2501.{index:05d}",
                    "title": f"Paper {index}",
                    "why": "Core evidence",
                }
                for index in range(101)
            ]
        ),
        encoding="utf-8",
    )
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )

    diagnostics.finalize_contract_audit()

    assert any(
        anomaly["kind"] == "plan_artifact_invalid"
        and anomaly["expected_artifact"] == "00_card_plan.json"
        for anomaly in diagnostics.snapshot()["anomalies"]
    )


def test_search_arguments_are_bounded_without_dropping_query(tmp_path: Path) -> None:
    sanitized = sanitize_tool_arguments(
        "scholight__search_papers",
        {
            "query": "q" * 700,
            "strength": "standard",
            "limit": 10,
            "authorization": "Bearer secret",
        },
        run_root=tmp_path,
    )

    assert sanitized["strength"] == "standard"
    assert sanitized["limit"] == 10
    assert str(sanitized["query"]).endswith("…")
    assert "authorization" not in sanitized
