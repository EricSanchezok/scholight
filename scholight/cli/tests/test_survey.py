"""Survey operator command contracts."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from click.testing import CliRunner

from scholight.cli.survey import (
    _diagnostic_projection,
    _installed_rcm_version,
    _verify_diagnostic_workspace,
    _verify_survey_runtime_schema,
    survey_group,
)
from scholight.config import settings


def test_installed_rcm_version_accepts_reviewed_binary() -> None:
    completed = subprocess.CompletedProcess(
        args=["/usr/local/bin/accelerate", "--version"],
        returncode=0,
        stdout="accelerate 0.2.8\n",
        stderr="",
    )
    with patch("scholight.cli.survey.subprocess.run", return_value=completed):
        version = _installed_rcm_version()

    assert version == "0.2.8"


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
    assert payload["conflict_count"] == 8


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
                "affected_components": ["expansion", "rank_pool"],
                "tool_counts": {"started": 4, "finished": 4, "failed": 0},
                "model_counts": {"started": 2, "finished": 1, "failed": 1},
                "last_model_error": {"error_code": "model_timeout", "timeout_seconds": 180},
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
    assert payload["affected_components"] == ["expansion", "rank_pool"]
    assert payload["model_counts"] == {"started": 2, "finished": 1, "failed": 1}
    assert payload["last_model_error"] == {
        "error_code": "model_timeout",
        "timeout_seconds": 180,
    }


def test_diagnostic_projection_classifies_bounded_stderr() -> None:
    job_id = uuid4()

    payload = _diagnostic_projection(
        {
            "process": {
                "return_code": 1,
                "termination_reason": "nonzero_exit",
                "stderr_tail": "provider returned status 429 rate limit",
            },
            "diagnostics": {"tool_counts": {"started": 1, "finished": 0, "failed": 1}},
        },
        job_id=job_id,
        source="workspace",
        location="diagnostics.json",
    )

    assert payload["stderr_classification"] == {
        "code": "survey_provider_rate_limited",
        "message": "A Survey provider is temporarily rate limited.",
    }


def test_smoke_diagnostic_workspace_probe_cleans_up(tmp_path: Path) -> None:
    _verify_diagnostic_workspace(tmp_path)

    assert list(tmp_path.iterdir()) == []


def _runtime_schema_query() -> str:
    pool = AsyncMock()
    with patch("scholight.cli.survey.get_pool", return_value=pool):
        asyncio.run(_verify_survey_runtime_schema())
    return str(pool.fetch.await_args.args[0])


def test_smoke_runtime_schema_probe_avoids_migration_table() -> None:
    assert "schema_migrations" not in _runtime_schema_query()


def test_smoke_runtime_schema_probe_checks_latest_survey_columns() -> None:
    query = _runtime_schema_query()

    assert all(
        column in query
        for column in (
            "surveys.title",
            "surveys.notify_on_completion",
            "drafts.request_hash",
            "jobs.cancel_requested_at",
            "notifications.status",
        )
    )
