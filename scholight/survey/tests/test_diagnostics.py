"""Runtime-owned Survey diagnostic trace and artifact contract tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

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
        "survey_assembler",
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
        "00_outline.md",
    ):
        (tmp_path / name).write_text("observed", encoding="utf-8")
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
        path.write_text("observed", encoding="utf-8")
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )

    diagnostics.finalize_contract_audit()

    assert diagnostics.snapshot()["first_anomaly"] == {
        "component": "survey_assembler",
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
