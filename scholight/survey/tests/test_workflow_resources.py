"""Runtime delivery tests for immutable Survey workflow resources."""

from __future__ import annotations

from pathlib import Path

import pytest

from scholight.survey.workflow_resources import (
    WorkflowResourceError,
    prepare_workflow_workspace,
    referenced_schema_paths,
    stage_workflow_schema,
)


def _workflow_root() -> Path:
    return Path(__file__).parents[1] / "workflow"


def test_stage_copies_every_schema_referenced_by_prompts(tmp_path: Path) -> None:
    staged = stage_workflow_schema(tmp_path, workflow_root=_workflow_root())

    referenced = referenced_schema_paths(_workflow_root())
    assert staged == referenced
    assert all((tmp_path / relative_path).is_file() for relative_path in referenced)


def test_stage_replaces_stale_schema_from_recovered_workspace(tmp_path: Path) -> None:
    stale = tmp_path / "schema" / "paper_card.md"
    stale.parent.mkdir()
    stale.write_text("stale", encoding="utf-8")

    stage_workflow_schema(tmp_path, workflow_root=_workflow_root())

    expected = (_workflow_root() / "schema" / "paper_card.md").read_text(encoding="utf-8")
    assert stale.read_text(encoding="utf-8") == expected


def test_stage_rejects_schema_symlink_in_workspace(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "schema").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkflowResourceError, match="symbolic link"):
        stage_workflow_schema(tmp_path, workflow_root=_workflow_root())


def test_stage_fails_when_prompt_references_missing_schema(tmp_path: Path) -> None:
    workflow = tmp_path / "workflow"
    (workflow / "prompts").mkdir(parents=True)
    (workflow / "schema").mkdir()
    (workflow / "prompts" / "broken.txt").write_text(
        "Read schema/missing.md.",
        encoding="utf-8",
    )
    run_root = tmp_path / "run"
    run_root.mkdir()

    with pytest.raises(WorkflowResourceError, match=r"missing\.md"):
        stage_workflow_schema(run_root, workflow_root=workflow)


def test_stage_wraps_a_missing_packaged_workflow(tmp_path: Path) -> None:
    with pytest.raises(WorkflowResourceError, match="unavailable"):
        stage_workflow_schema(tmp_path, workflow_root=tmp_path / "missing")


def test_stage_rejects_symlinked_packaged_prompt(tmp_path: Path) -> None:
    workflow = tmp_path / "workflow"
    prompt_root = workflow / "prompts"
    schema_root = workflow / "schema"
    prompt_root.mkdir(parents=True)
    schema_root.mkdir()
    (schema_root / "paper.md").write_text("contract", encoding="utf-8")
    outside_prompt = tmp_path / "outside.txt"
    outside_prompt.write_text("Read schema/paper.md.", encoding="utf-8")
    (prompt_root / "paper.txt").symlink_to(outside_prompt)
    run_root = tmp_path / "run"
    run_root.mkdir()

    with pytest.raises(WorkflowResourceError, match="unsafe"):
        stage_workflow_schema(run_root, workflow_root=workflow)


def test_prepare_workspace_creates_only_listable_contract_directories(tmp_path: Path) -> None:
    prepared = prepare_workflow_workspace(tmp_path)

    assert {path.name for path in prepared} == {
        "pdfs",
        "cards",
        "sections",
        "extracts",
        "reference_inputs",
        "reference_results",
        "shard_results",
    }
    assert all((tmp_path / name).is_dir() for name in ("pdfs", "cards", "sections", "extracts"))
    assert not (tmp_path / "03b_citation_expansion.md").exists()


def test_prepare_workspace_rejects_symlinked_contract_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "cards").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkflowResourceError, match="cards cannot be a symbolic link"):
        prepare_workflow_workspace(tmp_path)


def test_paper_card_prompt_requires_bounded_pdf_pagination_and_honest_truncation() -> None:
    prompt = (_workflow_root() / "prompts" / "paper_card.txt").read_text(encoding="utf-8")

    assert "limit=5000" in prompt
    assert "increasing `offset` values" in prompt
    assert "explicit end-of-file marker" in prompt
    assert "extraction-size cap" in prompt
    assert "Use `full_text` only after reaching end of file" in prompt
    assert "Use `partial` with reason `pdf_text_truncated`" in prompt
