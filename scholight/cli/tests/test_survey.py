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
    _run_image_canary,
    _run_model_canary,
    _verify_diagnostic_workspace,
    _verify_full_text_runtime,
    _verify_survey_runtime_schema,
    survey_group,
)
from scholight.config import settings
from scholight.survey.recovery import ArchivedSurveyRecovery


def test_installed_rcm_version_accepts_reviewed_binary() -> None:
    completed = subprocess.CompletedProcess(
        args=["/usr/local/bin/accelerate", "--version"],
        returncode=0,
        stdout="accelerate 0.2.16\n",
        stderr="",
    )
    with patch("scholight.cli.survey.subprocess.run", return_value=completed):
        version = _installed_rcm_version()

    assert version == "0.2.16"


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


def test_contract_audit_command_reports_no_known_warnings() -> None:
    result = CliRunner().invoke(survey_group, ["contract-audit", "--json-output"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["conflict_count"] == 0
    assert payload["conflicts"] == []


def test_archived_recovery_command_is_dry_run_by_default() -> None:
    job_id = uuid4()
    survey_id = uuid4()
    recovery = ArchivedSurveyRecovery(
        job_id=job_id,
        survey_id=survey_id,
        user_id=42,
        source_manifest_key=f"surveys/v1/42/{job_id}/manifest.json",
        source_manifest_sha256="c" * 64,
        manifest_key=f"surveys/v1/42/{job_id}/manifest.json",
        recovery_type="exact_report_reclassification",
        expected_manifest={"schema_version": 1},
        report_sha256="a" * 64,
        index_sha256="b" * 64,
        verified_file_count=12,
        contract_warning_count=1,
        applied=False,
        changed=False,
    )
    with (
        patch("scholight.cli.survey.create_pool", new_callable=AsyncMock),
        patch("scholight.cli.survey.close_pool", new_callable=AsyncMock),
        patch(
            "scholight.survey.recovery.recover_archived_survey",
            new_callable=AsyncMock,
            return_value=recovery,
        ) as recover,
    ):
        result = CliRunner().invoke(
            survey_group,
            ["recover-archived", str(job_id), "--json-output"],
        )

    assert result.exit_code == 0
    assert json.loads(result.output)["applied"] is False
    recover.assert_awaited_once_with(
        job_id=job_id,
        apply=False,
        expected_source_manifest_sha256=None,
        expected_report_sha256=None,
    )


def test_image_canary_reports_verified_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "data_root", str(tmp_path))

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        output = Path(args[-1])
        output.write_bytes(b"verified-image")
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps({"status": "ok", "size": len(b"verified-image")}),
            stderr="",
        )

    with (
        patch("scholight.cli.survey.subprocess.run", side_effect=_run),
        patch("scholight.cli.survey.emit_emf"),
    ):
        payload = _run_image_canary()

    assert payload["status"] == "ok"
    assert payload["size"] == len(b"verified-image")
    assert payload["provider_reason"] is None


def test_image_canary_exposes_only_sanitized_gateway_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    completed = subprocess.CompletedProcess(
        args=["accelerate", "image-canary"],
        returncode=1,
        stdout="",
        stderr=(
            "Error: image_gen_error code=image_request_rejected retryable=false "
            "http_status=400 provider_code=unsupported_parameter "
            "provider_reason=invalid_jwt sensitive-body"
        ),
    )
    with (
        patch("scholight.cli.survey.subprocess.run", return_value=completed),
        patch("scholight.cli.survey.emit_emf"),
    ):
        result = CliRunner().invoke(survey_group, ["image-canary", "--json-output"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error_code"] == "image_request_rejected"
    assert payload["http_status"] == 400
    assert payload["retryable"] is False
    assert payload["provider_code"] == "unsupported_parameter"
    assert payload["provider_reason"] == "invalid_jwt"
    assert "sensitive-body" not in result.output


def test_image_canary_timeout_is_a_structured_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    with (
        patch(
            "scholight.cli.survey.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["accelerate", "image-canary"], 780),
        ),
        patch("scholight.cli.survey.emit_emf") as emit,
    ):
        result = CliRunner().invoke(survey_group, ["image-canary", "--json-output"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error_code"] == "image_canary_timeout"
    assert payload["retryable"] is True
    assert payload["provider_reason"] is None
    emit.assert_called_once()


def test_model_canary_reports_protocol_success_without_model_text() -> None:
    completed = subprocess.CompletedProcess(
        args=["accelerate", "run"],
        returncode=0,
        stdout=(
            '{"type":"completion_end","outcome":"success","duration_ms":12}\n'
            '{"type":"tool_call","tool":"fs"}\n'
            '{"type":"tool_result","tool":"fs"}\n'
            '{"type":"appended","preview":"private model text"}\n'
            '{"type":"completion_end","outcome":"success","duration_ms":12}\n'
        ),
        stderr="private provider response",
    )
    with (
        patch("scholight.cli.survey.subprocess.run", return_value=completed),
        patch("scholight.cli.survey.emit_emf"),
    ):
        payload = _run_model_canary()

    assert payload["status"] == "ok"
    assert payload["error_code"] is None
    assert "private" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("stdout", "returncode"),
    [
        (
            '{"type":"completion_end","outcome":"success"}\n'
            '{"type":"completion_end","outcome":"success"}\n',
            0,
        ),
        (
            '{"type":"completion_end","outcome":"success"}\n'
            '{"type":"tool_call","tool":"fs"}\n'
            '{"type":"completion_end","outcome":"success"}\n',
            0,
        ),
    ],
)
def test_model_canary_rejects_runs_without_a_complete_tool_roundtrip(
    stdout: str,
    returncode: int,
) -> None:
    completed = subprocess.CompletedProcess(
        args=["accelerate", "run"],
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )
    with (
        patch("scholight.cli.survey.subprocess.run", return_value=completed),
        patch("scholight.cli.survey.emit_emf"),
    ):
        payload = _run_model_canary()

    assert payload["status"] == "failed"
    assert payload["error_code"] == "model_canary_tool_roundtrip_missing"


def test_model_canary_exposes_only_structured_provider_failure() -> None:
    completed = subprocess.CompletedProcess(
        args=["accelerate", "run"],
        returncode=1,
        stdout=(
            '{"type":"completion_end","outcome":"failure","http_status":503,'
            '"failure_kind":"provider_error","retryable":true,"duration_ms":120000}\n'
        ),
        stderr="private provider response body and key",
    )
    with (
        patch("scholight.cli.survey.subprocess.run", return_value=completed),
        patch("scholight.cli.survey.emit_emf"),
    ):
        result = CliRunner().invoke(survey_group, ["model-canary", "--json-output"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error_code"] == "model_provider_unavailable"
    assert payload["http_status"] == 503
    assert payload["retryable"] is True
    assert "private" not in result.output


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


def test_full_text_runtime_requires_accelerate_and_pdftotext() -> None:
    with (
        patch(
            "scholight.cli.survey.shutil.which",
            side_effect=lambda command: f"/usr/bin/{command}" if command == "accelerate" else None,
        ),
        pytest.raises(RuntimeError, match="pdftotext"),
    ):
        _verify_full_text_runtime()


def test_fulltext_canary_emits_only_status_and_character_count(tmp_path: Path) -> None:
    response = AsyncMock()
    response.content = b"%PDF-1.4 fixed public fixture"
    response.raise_for_status = lambda: None

    def _extract(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        Path(args[-1]).write_text("evidence " * 2_000, encoding="utf-8")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    with (
        patch.object(settings, "data_root", str(tmp_path)),
        patch("scholight.cli.survey.httpx.get", return_value=response),
        patch(
            "scholight.cli.survey._verify_full_text_runtime",
            return_value="/usr/bin/pdftotext",
        ),
        patch("scholight.cli.survey.subprocess.run", side_effect=_extract),
        patch("scholight.cli.survey.emit_emf") as emit,
    ):
        result = CliRunner().invoke(survey_group, ["fulltext-canary", "--json-output"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["characters"] >= 10_000
    assert set(payload) == {
        "characters",
        "duration_ms",
        "error_code",
        "schema_version",
        "status",
    }
    assert "evidence" not in result.output
    emit.assert_called_once()


@pytest.mark.parametrize(
    ("body", "extract_side_effect", "expected_error"),
    [
        (b"not a pdf", None, "fulltext_canary_pdf_invalid"),
        (
            b"%PDF-1.4 fixed public fixture",
            subprocess.TimeoutExpired(["pdftotext"], 60),
            "fulltext_canary_extraction_timeout",
        ),
    ],
)
def test_fulltext_canary_classifies_invalid_pdf_and_extraction_timeout(
    tmp_path: Path,
    body: bytes,
    extract_side_effect: BaseException | None,
    expected_error: str,
) -> None:
    response = AsyncMock()
    response.content = body
    response.raise_for_status = lambda: None
    with (
        patch.object(settings, "data_root", str(tmp_path)),
        patch("scholight.cli.survey.httpx.get", return_value=response),
        patch(
            "scholight.cli.survey._verify_full_text_runtime",
            return_value="/usr/bin/pdftotext",
        ),
        patch("scholight.cli.survey.subprocess.run", side_effect=extract_side_effect),
        patch("scholight.cli.survey.emit_emf"),
    ):
        result = CliRunner().invoke(survey_group, ["fulltext-canary", "--json-output"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error_code"] == expected_error
    assert payload["characters"] is None


def test_fulltext_canary_rejects_empty_extracted_text(tmp_path: Path) -> None:
    response = AsyncMock()
    response.content = b"%PDF-1.4 fixed public fixture"
    response.raise_for_status = lambda: None

    def _empty_extract(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        Path(args[-1]).write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    with (
        patch.object(settings, "data_root", str(tmp_path)),
        patch("scholight.cli.survey.httpx.get", return_value=response),
        patch(
            "scholight.cli.survey._verify_full_text_runtime",
            return_value="/usr/bin/pdftotext",
        ),
        patch("scholight.cli.survey.subprocess.run", side_effect=_empty_extract),
        patch("scholight.cli.survey.emit_emf"),
    ):
        result = CliRunner().invoke(survey_group, ["fulltext-canary", "--json-output"])

    assert result.exit_code == 1
    assert json.loads(result.output)["error_code"] == "fulltext_canary_text_too_short"


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
