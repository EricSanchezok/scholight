"""Resumable Survey stage planning and reference-shard contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scholight.survey.resumable_runner import (
    SurveyStageContractError,
    bibliography_excerpt,
    load_card_plan,
    load_section_plan,
    merge_reference_shards,
)


def test_load_plans_validate_ids_and_paths(tmp_path: Path) -> None:
    (tmp_path / "00_card_plan.json").write_text(
        json.dumps(
            [
                {
                    "run_dir": ".",
                    "id": "2401.01234",
                    "title": "A paper",
                    "why": "core method",
                },
                {
                    "run_dir": ".",
                    "id": "cs/0012009",
                    "title": "A legacy paper",
                    "why": "historical anchor",
                },
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
                    "thesis": "Set the field boundary.",
                    "card_ids": ["2401.01234"],
                    "transfer_angle": "",
                }
            ]
        ),
        encoding="utf-8",
    )

    cards = load_card_plan(tmp_path)
    sections = load_section_plan(tmp_path, card_ids={item["id"] for item in cards})

    assert [item["stem"] for item in cards] == ["2401.01234", "cs-0012009"]
    assert sections[0]["artifact"] == "sections/01_introduction.md"


def test_section_plan_rejects_unplanned_card(tmp_path: Path) -> None:
    (tmp_path / "00_sections.json").write_text(
        json.dumps(
            [
                {
                    "run_dir": ".",
                    "n": "01",
                    "slug": "escape",
                    "title": "Bad",
                    "thesis": "Bad",
                    "card_ids": ["2401.99999"],
                    "transfer_angle": "",
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(SurveyStageContractError, match="unplanned card"):
        load_section_plan(tmp_path, card_ids={"2401.01234"})


def test_bibliography_excerpt_is_bounded_and_records_truncation() -> None:
    body = b"Introduction\n" + b"x" * 600_000 + b"\nReferences\n" + b"r" * 700_000

    excerpt, truncated = bibliography_excerpt(body, maximum_bytes=512 * 1024)

    assert len(excerpt.encode("utf-8")) <= 512 * 1024
    assert excerpt.startswith("References")
    assert truncated


def test_reference_merger_preserves_one_result_per_seed(tmp_path: Path) -> None:
    results = tmp_path / "reference_results"
    results.mkdir()
    (results / "2401.00001.md").write_text(
        "# Reference seed 2401.00001\nstatus: completed\n\n- [2401.10000] Result\n",
        encoding="utf-8",
    )
    (results / "2401.00002.md").write_text(
        "# Reference seed 2401.00002\nstatus: failed\nreason: provider_request_rejected\n",
        encoding="utf-8",
    )

    summary = merge_reference_shards(
        tmp_path,
        (("2401.00001", "2401.00001"), ("2401.00002", "2401.00002")),
    )

    output = (tmp_path / "03b_citation_expansion.md").read_text(encoding="utf-8")
    assert summary == {"completed": 1, "failed": 1}
    assert "2401.00001" in output
    assert "2401.00002" in output
