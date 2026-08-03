"""Read-only audit of known Survey workflow definition conflicts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class WorkflowConflict:
    code: str
    summary: str
    evidence: tuple[str, ...]
    severity: str = "warning"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _read(relative_path: str) -> str:
    return (_PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def audit_workflow_contracts() -> tuple[WorkflowConflict, ...]:
    """Return currently observable conflicts without changing or blocking the workflow."""
    conflicts: list[WorkflowConflict] = []
    card_schema = _read("scholight/survey/workflow/schema/card_plan.md")
    card_prompt = _read("scholight/survey/workflow/prompts/card_plan.txt")
    normalized_card_prompt = " ".join(card_prompt.split())
    if "00_card_plan.json" in card_schema and "Do NOT write 00_card_plan.json" in card_prompt:
        conflicts.append(
            WorkflowConflict(
                code="card_plan_definition_conflict",
                summary="Card plan schema requires a file that the active spawn prompt forbids.",
                evidence=("schema/card_plan.md", "prompts/card_plan.txt"),
            )
        )

    section_schema = _read("scholight/survey/workflow/schema/section.md")
    section_prompt = _read("scholight/survey/workflow/prompts/survey_outline.txt")
    normalized_section_prompt = " ".join(section_prompt.split())
    if "00_sections.json" in section_schema and "Do NOT write 00_sections.json" in section_prompt:
        conflicts.append(
            WorkflowConflict(
                code="section_definition_conflict",
                summary="Section schema requires a file that the active spawn prompt forbids.",
                evidence=("schema/section.md", "prompts/survey_outline.txt"),
            )
        )

    handoff = _read("scholight/survey/workflow/schema/handoff.md")
    image_prompt = _read("scholight/survey/workflow/prompts/image_planner.txt")
    if "`ok` | `partial` | `blocked`" in handoff and "status degraded" in image_prompt:
        conflicts.append(
            WorkflowConflict(
                code="image_status_enum_conflict",
                summary="Image failure uses a handoff status outside the declared enum.",
                evidence=("schema/handoff.md", "prompts/image_planner.txt"),
            )
        )

    judge_schema = _read("scholight/survey/workflow/schema/judge_panel.md")
    if "strong, acceptable, insufficient, or blocked" in judge_schema:
        conflicts.append(
            WorkflowConflict(
                code="judge_verdict_unvalidated",
                summary="Judge verdicts are declared in prose but are not runtime validated.",
                evidence=("schema/judge_panel.md",),
            )
        )

    pipeline = _read("scholight/survey/workflow/rcm/survey_pipeline.rcm")
    agent_guide = _read("scholight/survey/workflow/AGENTS.md")
    if ".done ->" in pipeline and "artifact on disk is the source of truth" in agent_guide:
        conflicts.append(
            WorkflowConflict(
                code="completion_artifact_gap",
                summary="RCM completion edges do not prove that the promised artifact exists.",
                evidence=("rcm/survey_pipeline.rcm", "AGENTS.md"),
            )
        )

    expansion_schema = _read("scholight/survey/workflow/schema/expansion.md")
    reference_prompt = _read("scholight/survey/workflow/prompts/reference_expander.txt")
    if "result: empty" not in expansion_schema or "result: empty" not in reference_prompt:
        conflicts.append(
            WorkflowConflict(
                code="empty_artifact_undefined",
                summary="A valid empty citation result is not distinguished from a missing file.",
                evidence=("schema/expansion.md", "prompts/reference_expander.txt"),
            )
        )

    if not (
        "write run_dir/00_card_plan.json before spawning" in normalized_card_prompt
        and "write run_dir/00_sections.json before spawning" in normalized_section_prompt
    ):
        conflicts.append(
            WorkflowConflict(
                code="spawn_expectations_not_persisted",
                summary="Spawned card and section expectations have no durable plan artifact.",
                evidence=("prompts/card_plan.txt", "prompts/survey_outline.txt"),
            )
        )

    worker = _read("scholight/survey/worker.py")
    diagnostics = _read("scholight/survey/diagnostics.py")
    if not (
        'ArtifactContract("survey_assembler", required=("08_survey.md", "index.md"))' in diagnostics
        and "survey_contract_violation" in worker
        and "section_missing_from_final_report" in diagnostics
        and "references_missing_from_final_report" in diagnostics
    ):
        conflicts.append(
            WorkflowConflict(
                code="final_report_validation_incomplete",
                summary="Worker success validation does not prove that final output is complete.",
                evidence=("worker.py", "diagnostics.py", "prompts/survey_assembler.txt"),
            )
        )

    if "component_start" in worker and "update_survey_job_progress" in worker:
        conflicts.append(
            WorkflowConflict(
                code="progress_stream_dependency",
                summary="Persisted progress depends on receiving RCM component_start events.",
                evidence=("worker.py", "progress.py"),
            )
        )

    return tuple(sorted(conflicts, key=lambda conflict: conflict.code))


def workflow_audit_payload() -> dict[str, object]:
    conflicts = audit_workflow_contracts()
    return {
        "schema_version": 1,
        "status": "warning" if conflicts else "ok",
        "conflict_count": len(conflicts),
        "conflicts": [conflict.as_dict() for conflict in conflicts],
    }


__all__ = ["WorkflowConflict", "audit_workflow_contracts", "workflow_audit_payload"]
