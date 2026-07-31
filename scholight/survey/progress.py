"""Stable user-facing Survey progress derived from internal RCM components."""

from __future__ import annotations

import re
from typing import Literal

ExecutionProgressStage = Literal[
    "waiting",
    "planning",
    "discovering",
    "reviewing_evidence",
    "structuring_report",
    "writing_report",
    "finalizing",
]
PublicProgressStage = Literal[
    "drafting",
    "waiting",
    "planning",
    "discovering",
    "reviewing_evidence",
    "structuring_report",
    "writing_report",
    "finalizing",
    "saving_results",
    "completed",
    "cancelled",
]

EXECUTION_PROGRESS_STAGES: tuple[ExecutionProgressStage, ...] = (
    "waiting",
    "planning",
    "discovering",
    "reviewing_evidence",
    "structuring_report",
    "writing_report",
    "finalizing",
)
TOTAL_PROGRESS_STEPS = 8

_STAGE_PRESENTATION: dict[PublicProgressStage, tuple[int, int]] = {
    "drafting": (0, 0),
    "waiting": (0, 0),
    "planning": (8, 1),
    "discovering": (25, 2),
    "reviewing_evidence": (55, 3),
    "structuring_report": (75, 4),
    "writing_report": (88, 5),
    "finalizing": (96, 6),
    "saving_results": (98, 7),
    "completed": (100, 8),
    "cancelled": (0, 0),
}

_COMPONENT_STAGES: dict[str, ExecutionProgressStage] = {
    "anchor": "planning",
    "query_plan": "planning",
    "discovery": "discovering",
    "method_scout": "discovering",
    "benchmark_scout": "discovering",
    "survey_scout": "discovering",
    "frontier_scout": "discovering",
    "discovery_merger": "discovering",
    "expansion": "reviewing_evidence",
    "citation_seed_selector": "reviewing_evidence",
    "reference_expander": "reviewing_evidence",
    "semantic_expander": "reviewing_evidence",
    "cross_domain_expander": "reviewing_evidence",
    "expansion_merger": "reviewing_evidence",
    "rank_pool": "reviewing_evidence",
    "card_plan": "reviewing_evidence",
    "paper_card": "reviewing_evidence",
    "research_map": "reviewing_evidence",
    "judge_panel": "reviewing_evidence",
    "coverage_judge": "reviewing_evidence",
    "scope_judge": "reviewing_evidence",
    "benchmark_judge": "reviewing_evidence",
    "gap_judge": "reviewing_evidence",
    "judge_synthesizer": "reviewing_evidence",
    "image_planner": "structuring_report",
    "survey_outline": "structuring_report",
    "section_expander": "writing_report",
    "survey_assembler": "finalizing",
}


def stage_for_component(name: str) -> ExecutionProgressStage | None:
    """Map an internal component name without exposing pipeline internals to clients."""
    normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", name).replace("-", "_").casefold()
    return _COMPONENT_STAGES.get(normalized)


def present_progress(
    *, survey_status: str, execution_stage: str | None
) -> tuple[PublicProgressStage, int, int]:
    """Return the public stage, milestone percentage, and completed step number."""
    if survey_status == "drafting":
        stage: PublicProgressStage = "drafting"
    elif survey_status == "queued":
        stage = "waiting"
    elif survey_status == "archiving":
        stage = "saving_results"
    elif survey_status == "succeeded":
        stage = "completed"
    elif survey_status == "cancelled":
        stage = "cancelled"
    elif execution_stage in _STAGE_PRESENTATION:
        stage = execution_stage
    else:
        stage = "planning" if survey_status == "running" else "waiting"
    percent, step = _STAGE_PRESENTATION[stage]
    return stage, percent, step


__all__ = [
    "EXECUTION_PROGRESS_STAGES",
    "ExecutionProgressStage",
    "PublicProgressStage",
    "TOTAL_PROGRESS_STEPS",
    "present_progress",
    "stage_for_component",
]
