"""Resumable Survey stage planning and reference-shard contracts."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from scholight.survey.durable_workflow import DurableUnit
from scholight.survey.process import ProcessControl
from scholight.survey.resumable_runner import (
    SurveyStageContractError,
    _run_rcm_once,
    _run_reference_seed,
    _StageProcessError,
    bibliography_excerpt,
    load_card_plan,
    load_section_plan,
    merge_reference_shards,
)


class _CompletedProcess:
    def __init__(self, *, stdout: bytes, stderr: bytes = b"") -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.stdin = None
        self.returncode = 0

    async def wait(self) -> int:
        return 0


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


@pytest.mark.asyncio
async def test_zero_exit_terminal_completion_failure_is_raised_from_structured_stdout(
    tmp_path: Path,
) -> None:
    process = _CompletedProcess(
        stdout=(
            json.dumps(
                {
                    "type": "completion_end",
                    "outcome": "failure",
                    "http_status": 400,
                    "failure_kind": "invalid_request",
                    "retryable": False,
                    "request_class": "request_size",
                    "serialized_request_bytes": 900_000,
                }
            ).encode()
            + b"\n"
        )
    )
    unit = DurableUnit("reference_seed:2401.00001", "reference_seed.rcm", "{}", ())
    attempt_id = uuid4()
    record_diagnostics = AsyncMock()

    with (
        patch(
            "scholight.survey.resumable_runner.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ),
        patch(
            "scholight.survey.resumable_runner.write_stdin",
            new_callable=AsyncMock,
        ),
        patch("scholight.survey.resumable_runner.survey_environment", return_value={}),
        patch(
            "scholight.survey.resumable_runner.record_compute_attempt_diagnostics",
            record_diagnostics,
        ),
    ):
        with pytest.raises(_StageProcessError) as raised:
            await _run_rcm_once(
                unit=unit,
                job=SimpleNamespace(user_id=42, lease_owner=attempt_id),  # type: ignore[arg-type]
                run_root=tmp_path,
                control=ProcessControl(),
                deadline=datetime.now(UTC) + timedelta(minutes=1),
            )

    assert raised.value.code == "survey_model_request_rejected"
    assert raised.value.diagnostics == {
        "failure_kind": "invalid_request",
        "http_status": 400,
        "retryable": False,
        "request_class": "request_size",
        "serialized_request_bytes": 900_000,
    }
    assert raised.value.stderr_tail == ""
    record_diagnostics.assert_awaited_once_with(
        attempt_id=attempt_id,
        failure_class="provider_request_size",
        failure_details={
            "provider_status": 400,
            "provider_request_class": "request_size",
            "request_bytes": 900_000,
        },
    )


@pytest.mark.asyncio
async def test_reference_seed_uses_structured_size_class_for_one_shrink_retry(
    tmp_path: Path,
) -> None:
    inputs = tmp_path / "reference_inputs"
    results = tmp_path / "reference_results"
    inputs.mkdir()
    results.mkdir()
    input_path = inputs / "2401.00001.json"
    input_path.write_text(
        json.dumps({"bibliography": "r" * 10_000, "truncated": False}),
        encoding="utf-8",
    )
    (results / "2401.00001.md").write_text(
        "# Reference seed 2401.00001\nstatus: completed\n",
        encoding="utf-8",
    )
    initial_failure = _StageProcessError(
        "survey_model_request_rejected",
        "rejected",
        diagnostics={"request_class": "request_size", "http_status": 400},
    )
    retry_once = AsyncMock()
    unit = DurableUnit(
        "reference_seed:2401.00001",
        "reference_seed.rcm",
        json.dumps({"seed_id": "2401.00001", "stem": "2401.00001"}),
        (),
    )

    with (
        patch("scholight.survey.resumable_runner._prepare_reference_input", return_value=True),
        patch(
            "scholight.survey.resumable_runner._run_rcm_with_retries",
            new_callable=AsyncMock,
            side_effect=initial_failure,
        ),
        patch("scholight.survey.resumable_runner._run_rcm_once", retry_once),
    ):
        diagnostics = await _run_reference_seed(
            unit=unit,
            job=SimpleNamespace(user_id=42),  # type: ignore[arg-type]
            run_root=tmp_path,
            control=ProcessControl(),
            deadline=datetime.now(UTC) + timedelta(minutes=1),
        )

    shrunk = json.loads(input_path.read_text(encoding="utf-8"))
    assert diagnostics == {"request_class": "request_size", "http_status": 400}
    assert shrunk["truncated"] is True
    assert len(shrunk["bibliography"]) == 5000
    retry_once.assert_awaited_once()
