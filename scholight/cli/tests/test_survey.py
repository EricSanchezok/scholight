"""Survey operator command contracts."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from click.testing import CliRunner

from scholight.cli.survey import (
    _installed_rcm_version,
    _verify_diagnostic_workspace,
    survey_group,
)
from scholight.config import settings


def test_installed_rcm_version_accepts_reviewed_binary() -> None:
    completed = subprocess.CompletedProcess(
        args=["/usr/local/bin/accelerate", "--version"],
        returncode=0,
        stdout="accelerate 0.2.6\n",
        stderr="",
    )
    with patch("scholight.cli.survey.subprocess.run", return_value=completed):
        version = _installed_rcm_version()

    assert version == "0.2.6"


def test_installed_rcm_version_rejects_unreviewed_binary() -> None:
    completed = subprocess.CompletedProcess(
        args=["/usr/local/bin/accelerate", "--version"],
        returncode=0,
        stdout="accelerate 0.2.5\n",
        stderr="",
    )
    with (
        patch("scholight.cli.survey.subprocess.run", return_value=completed),
        pytest.raises(RuntimeError, match="reviewed release"),
    ):
        _installed_rcm_version()


def test_contract_audit_command_reports_known_warnings() -> None:
    result = CliRunner().invoke(survey_group, ["contract-audit", "--json-output"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "warning"
    assert payload["conflict_count"] == 10


def test_diagnose_reads_active_workspace_without_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    run_root = tmp_path / "surveys" / str(job_id) / "run"
    run_root.mkdir(parents=True)
    (run_root / "diagnostics.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": str(job_id),
                "last_successful_component": "query_plan",
                "first_anomaly": {
                    "component": "discovery_merger",
                    "expected_artifact": "02_candidate_pool.md",
                },
                "anomaly_count": 1,
                "tool_counts": {"started": 4, "finished": 4, "failed": 0},
                "trace_path": "trajectory.jsonl",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "data_root", tmp_path)

    result = CliRunner().invoke(
        survey_group,
        ["diagnose", str(job_id), "--json-output"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["source"] == "workspace"
    assert payload["last_successful_component"] == "query_plan"
    assert payload["first_anomaly"]["expected_artifact"] == "02_candidate_pool.md"


def test_smoke_diagnostic_workspace_probe_cleans_up(tmp_path: Path) -> None:
    _verify_diagnostic_workspace(tmp_path)

    assert list(tmp_path.iterdir()) == []
