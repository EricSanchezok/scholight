"""Runtime delivery tests for immutable Survey workflow resources."""

from __future__ import annotations

from pathlib import Path

import pytest

from scholight.survey.workflow_resources import (
    WorkflowResourceError,
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
