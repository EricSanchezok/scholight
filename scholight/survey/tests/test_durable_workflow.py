"""Durable Survey unit scheduling and artifact gates."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from scholight.survey.durable_workflow import (
    ArtifactContract,
    DurableSurveyExecutor,
    DurableUnit,
    SurveyArtifactContractError,
)


@pytest.mark.asyncio
async def test_missing_artifact_blocks_checkpoint_and_downstream(tmp_path: Path) -> None:
    called: list[str] = []
    checkpoints: list[str] = []

    async def run(unit: DurableUnit) -> None:
        called.append(unit.name)

    async def checkpoint(unit: str, _completed: tuple[str, ...]) -> None:
        checkpoints.append(unit)

    executor = DurableSurveyExecutor(
        run_root=tmp_path,
        completed_units=(),
        run_unit=run,
        checkpoint=checkpoint,
    )
    with pytest.raises(SurveyArtifactContractError, match=r"01_query_plan\.md"):
        await executor.execute(
            DurableUnit(
                name="query_plan",
                workflow="query_plan.rcm",
                purpose="survey",
                artifacts=(ArtifactContract("01_query_plan.md"),),
            )
        )
    assert called == ["query_plan"]
    assert checkpoints == []


@pytest.mark.asyncio
async def test_fanout_checkpoints_each_unit_and_resume_skips_completed(tmp_path: Path) -> None:
    called: list[str] = []
    checkpoints: list[tuple[str, tuple[str, ...]]] = []

    async def run(unit: DurableUnit) -> None:
        called.append(unit.name)
        await asyncio.sleep(0)
        path = tmp_path / unit.artifacts[0].path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {unit.name}\n", encoding="utf-8")

    async def checkpoint(unit: str, completed: tuple[str, ...]) -> None:
        checkpoints.append((unit, completed))

    already = tmp_path / "cards" / "one.md"
    already.parent.mkdir()
    already.write_text("# existing\n", encoding="utf-8")
    executor = DurableSurveyExecutor(
        run_root=tmp_path,
        completed_units=("paper_card:one",),
        run_unit=run,
        checkpoint=checkpoint,
    )
    units = (
        DurableUnit(
            name="paper_card:one",
            workflow="paper_card.rcm",
            purpose="one",
            artifacts=(ArtifactContract("cards/one.md"),),
        ),
        DurableUnit(
            name="paper_card:two",
            workflow="paper_card.rcm",
            purpose="two",
            artifacts=(ArtifactContract("cards/two.md"),),
        ),
        DurableUnit(
            name="paper_card:three",
            workflow="paper_card.rcm",
            purpose="three",
            artifacts=(ArtifactContract("cards/three.md"),),
        ),
    )

    await executor.execute_many(units, concurrency=2)

    assert sorted(called) == ["paper_card:three", "paper_card:two"]
    assert {unit for unit, _completed in checkpoints} == {
        "paper_card:two",
        "paper_card:three",
    }
    assert executor.completed_units == (
        "paper_card:one",
        "paper_card:three",
        "paper_card:two",
    )
