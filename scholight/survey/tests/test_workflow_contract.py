"""Static contracts for the vendored Scholight Survey RCM workflow."""

import re
from pathlib import Path

import pytest

from scholight.survey import workflow_audit
from scholight.survey.workflow_audit import audit_workflow_contracts

_WORKFLOW = Path(__file__).parents[1] / "workflow"


def _text_files() -> list[Path]:
    return sorted(
        path
        for path in _WORKFLOW.rglob("*")
        if path.is_file() and path.suffix in {".md", ".rcm", ".toml", ".txt"}
    )


def test_workflow_has_no_legacy_search_or_translation_branch() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in _text_files())

    assert "arxiv_search" not in combined
    assert "webfetch" not in combined
    assert "OPENAI_API_KEY" not in combined
    assert not any("zh_" in path.name for path in _text_files())
    assert not (_WORKFLOW / "rcm" / "section_translator.rcm").exists()
    assert not (_WORKFLOW / "rcm" / "zh_assemble.rcm").exists()


def test_discovery_and_expansion_use_authenticated_scholight_mcp() -> None:
    for name in ("discovery.rcm", "expansion.rcm"):
        source = (_WORKFLOW / "rcm" / name).read_text(encoding="utf-8")
        assert 'url = "http://api:8000/mcp"' in source
        assert 'env "SCHOLIGHT_SURVEY_MCP_AUTHORIZATION"' in source
        assert 'mcps = ["scholight"]' in source


def test_every_survey_model_allows_at_least_thirty_minutes() -> None:
    model_files = [
        path
        for path in sorted((_WORKFLOW / "rcm").glob("*.rcm"))
        if not path.stem.endswith("_canary")
        and re.search(r"(?m)^model\s+", path.read_text(encoding="utf-8"))
    ]

    assert model_files
    for path in model_files:
        source = path.read_text(encoding="utf-8")
        timeouts = [int(value) for value in re.findall(r'(?m)^\s*timeout\s*=\s*"(\d+)"', source)]
        model_count = len(re.findall(r"(?m)^model\s+", source))
        assert len(timeouts) == model_count, f"{path.name} must set every model timeout"
        assert min(timeouts) >= 1_800, f"{path.name} model timeout is below 30 minutes"


def test_model_canary_has_a_short_bounded_timeout() -> None:
    source = (_WORKFLOW / "rcm" / "model_canary.rcm").read_text(encoding="utf-8")

    assert re.findall(r'(?m)^\s*timeout\s*=\s*"(\d+)"', source) == ["120"]
    assert 'limit = { context = "4096", output = "512" }' in source
    assert 'thinking = "true"' in source
    assert 'tools = ["fs"]' in source


def test_deepseek_workflows_enable_thinking_tool_history_compatibility() -> None:
    model_files = [
        path
        for path in sorted((_WORKFLOW / "rcm").glob("*.rcm"))
        if "model deepseek-v4-flash" in path.read_text(encoding="utf-8")
    ]

    assert model_files
    for path in model_files:
        source = path.read_text(encoding="utf-8")
        model_count = len(re.findall(r"(?m)^model\s+deepseek-v4-flash\s*\{", source))
        thinking_count = len(re.findall(r'(?m)^\s*thinking\s*=\s*"true"', source))
        assert thinking_count == model_count, (
            f"{path.name} must preserve DeepSeek reasoning_content across tool turns"
        )


def test_draft_workflow_is_single_node_mcp_only() -> None:
    source = (_WORKFLOW / "rcm" / "draft.rcm").read_text(encoding="utf-8")

    assert 'url = "http://api:8000/mcp"' in source
    assert 'env "SCHOLIGHT_SURVEY_MCP_AUTHORIZATION"' in source
    assert 'mcps = ["scholight"]' in source
    assert "graph {" not in source
    assert "tools =" not in source
    assert '"fs"' not in source


def test_draft_prompt_returns_markdown_without_artifact_files() -> None:
    source = (_WORKFLOW / "prompts" / "draft.txt").read_text(encoding="utf-8")

    assert "scholight__search_papers" in source
    assert "Markdown" in source
    assert "Return only a concise Markdown Draft" in source
    assert "write" not in source.lower()


def test_draft_prompt_is_concise_positive_and_approval_ready() -> None:
    source = (_WORKFLOW / "prompts" / "draft.txt").read_text(encoding="utf-8")
    normalized = " ".join(source.split())

    assert "Use qualitative depth by default" in normalized
    assert "instead of collecting them in a separate section" in normalized
    assert "Leave language and numeric targets open unless the user sets them" in normalized
    assert "ready to approve and execute, not a questionnaire" in normalized
    assert source.count("Do not") <= 1


def test_search_strengths_match_survey_retrieval_policy() -> None:
    prompts = _WORKFLOW / "prompts"
    for name in (
        "method_scout.txt",
        "benchmark_scout.txt",
        "survey_scout.txt",
        "frontier_scout.txt",
        "semantic_expander.txt",
        "cross_domain_expander.txt",
    ):
        source = (prompts / name).read_text(encoding="utf-8")
        assert "scholight__search_papers" in source
        assert 'strength="standard"' in source
        assert "limit=10" in source

    reference = (prompts / "reference_expander.txt").read_text(encoding="utf-8")
    assert "scholight__search_papers" in reference
    assert 'strength="thorough"' in reference
    assert "limit=5" in reference
    assert "arxiv_download" in reference


def test_cli_run_directory_is_the_single_artifact_root() -> None:
    anchor = (_WORKFLOW / "prompts" / "anchor.txt").read_text(encoding="utf-8")
    handoff = (_WORKFLOW / "schema" / "handoff.md").read_text(encoding="utf-8")

    assert "use `.` as run_dir" in anchor
    assert "every node must use `.`" in handoff
    assert "return `status: blocked`" in handoff


def test_workflow_cannot_discover_workspaces_or_execute_shell() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in _text_files())

    assert '"shell"' not in combined
    assert "newest run" not in combined
    assert "runs/<timestamp>" not in combined


def test_english_report_is_the_only_final_assembly_output() -> None:
    prompts = _WORKFLOW / "prompts"
    writers = [
        path.name
        for path in prompts.glob("*.txt")
        if "08_survey.md" in path.read_text(encoding="utf-8")
    ]
    pipeline = (_WORKFLOW / "rcm" / "survey_pipeline.rcm").read_text(encoding="utf-8")
    schema = (_WORKFLOW / "schema" / "survey.md").read_text(encoding="utf-8")

    assert writers == []
    assert "SurveyAssembler" not in pipeline
    assert not (prompts / "survey_assembler.txt").exists()
    assert "Final assembly is application-owned and deterministic" in " ".join(schema.split())
    assert "status: partial" in (prompts / "image_planner.txt").read_text(encoding="utf-8")


def test_fan_out_plans_are_durable_and_restart_safe() -> None:
    prompts = _WORKFLOW / "prompts"
    card_plan = (prompts / "card_plan.txt").read_text(encoding="utf-8")
    section_plan = (prompts / "survey_outline.txt").read_text(encoding="utf-8")
    normalized_card_plan = " ".join(card_plan.split())
    normalized_section_plan = " ".join(section_plan.split())

    assert "write run_dir/00_card_plan.json before spawning" in normalized_card_plan
    assert "Exclude cards that already exist" in card_plan
    assert "one retry call" in card_plan
    assert "write run_dir/00_sections.json before spawning" in normalized_section_plan
    assert "write run_dir/00_outline.json before" in normalized_section_plan
    assert "schema/outline.md" in section_plan
    assert "exact canonical `id` values from `00_card_plan.json`" in normalized_section_plan
    assert "Exclude section files that already exist" in section_plan
    assert "one retry call" in section_plan


def test_empty_citation_expansion_has_an_explicit_artifact_state() -> None:
    reference = (_WORKFLOW / "prompts" / "reference_expander.txt").read_text(encoding="utf-8")
    expansion = (_WORKFLOW / "schema" / "expansion.md").read_text(encoding="utf-8")

    assert "result: empty" in reference
    assert "result: empty" in expansion


def test_contract_audit_classifies_every_known_definition_gap() -> None:
    codes = {conflict.code for conflict in audit_workflow_contracts()}

    assert codes == set()


def test_contract_audit_does_not_depend_on_test_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_project_file = workflow_audit._read

    def read_production_file(relative_path: str) -> str:
        if relative_path.startswith("tests/"):
            raise FileNotFoundError(relative_path)
        return read_project_file(relative_path)

    monkeypatch.setattr(workflow_audit, "_read", read_production_file)

    assert audit_workflow_contracts() == ()


def test_e2e_uses_vendored_graph_and_only_redirects_model_transport() -> None:
    dockerfile = (Path(__file__).parents[3] / "tests/survey_e2e/Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "tests/survey_e2e/workflows" not in dockerfile
    assert "ENV PYTHONPATH=/app" in dockerfile
    assert "https://api.deepseek.com" in dockerfile
    assert "http://model:8080/v1" in dockerfile


def test_e2e_waits_for_fake_model_health_before_starting_api() -> None:
    compose = (Path(__file__).parents[3] / "tests/survey_e2e/compose.yaml").read_text(
        encoding="utf-8"
    )

    model_service = compose.split("\n  model:\n", 1)[1].split("\n  api:\n", 1)[0]
    api_service = compose.split("\n  api:\n", 1)[1].split("\n  survey-draft-worker:\n", 1)[0]
    assert "http://127.0.0.1:8080/health" in model_service
    assert "condition: service_healthy" in api_service.split("model:", 1)[1]
