"""Static contracts for the vendored Scholight Survey RCM workflow."""

from pathlib import Path

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
    assert "final assistant message" in source
    assert "write" not in source.lower()


def test_draft_prompt_keeps_requirements_proportional_and_assumptions_local() -> None:
    source = (_WORKFLOW / "prompts" / "draft.txt").read_text(encoding="utf-8")
    normalized = " ".join(source.split())

    assert "Do not invent numeric targets" in normalized
    assert "Do not infer an output language" in normalized
    assert "Do not create a standalone assumptions or uncertainty section" in normalized
    assert "Integrate each material assumption" in normalized


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
    assembler = (prompts / "survey_assembler.txt").read_text(encoding="utf-8")

    assert writers == ["survey_assembler.txt"]
    assert "sole final report" in " ".join(assembler.split())
    assert "status degraded" in (prompts / "image_planner.txt").read_text(encoding="utf-8")
