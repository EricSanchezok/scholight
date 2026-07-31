"""Stable public progress stages derived from internal RCM component events."""

from scholight.survey.progress import present_progress, stage_for_component


def test_internal_components_map_to_stable_public_stages() -> None:
    assert stage_for_component("query_plan") == "planning"
    assert stage_for_component("discovery") == "discovering"
    assert stage_for_component("PaperCard") == "reviewing_evidence"
    assert stage_for_component("survey_outline") == "structuring_report"
    assert stage_for_component("SectionExpander") == "writing_report"
    assert stage_for_component("survey_assembler") == "finalizing"


def test_unknown_component_does_not_invent_progress() -> None:
    assert stage_for_component("future_internal_helper") is None


def test_archiving_and_terminal_states_override_execution_stage() -> None:
    assert present_progress(survey_status="archiving", execution_stage="writing_report") == (
        "saving_results",
        98,
        7,
    )
    assert present_progress(survey_status="succeeded", execution_stage="finalizing") == (
        "completed",
        100,
        8,
    )


def test_failed_survey_keeps_last_real_execution_milestone() -> None:
    assert present_progress(survey_status="failed", execution_stage="reviewing_evidence") == (
        "reviewing_evidence",
        55,
        3,
    )
