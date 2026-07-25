from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[2] / "scripts" / "production_canary.py"
_SPEC = importlib.util.spec_from_file_location("production_canary", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

RequestResult = _MODULE.RequestResult
StageSummary = _MODULE.StageSummary
build_rate_plan = _MODULE.build_rate_plan
evaluate_stage = _MODULE.evaluate_stage
percentile = _MODULE.percentile
validate_target = _MODULE.validate_target
validate_load_limits = _MODULE.validate_load_limits
write_report = _MODULE.write_report


def test_rate_plan_stops_at_requested_maximum() -> None:
    assert build_rate_plan(2.0, candidates=(0.5, 1.0, 2.0, 4.0)) == (0.5, 1.0, 2.0)


def test_rate_plan_includes_nonstandard_maximum() -> None:
    assert build_rate_plan(3.0, candidates=(0.5, 1.0, 2.0, 4.0)) == (0.5, 1.0, 2.0, 3.0)


def test_remote_target_requires_explicit_confirmation() -> None:
    with pytest.raises(ValueError, match="--allow-remote"):
        validate_target("https://scholight.example.com", allow_remote=False)


def test_loopback_target_does_not_require_remote_confirmation() -> None:
    assert validate_target("http://127.0.0.1:8000", allow_remote=False) == ("http://127.0.0.1:8000")


def test_elevated_standard_load_requires_explicit_confirmation() -> None:
    with pytest.raises(ValueError, match="--allow-elevated-load"):
        validate_load_limits(
            maximum_standard_rps=40.0,
            maximum_thorough_rps=None,
            allow_elevated_load=False,
        )


def test_elevated_load_allows_tenfold_profile() -> None:
    validate_load_limits(
        maximum_standard_rps=40.0,
        maximum_thorough_rps=4.0,
        allow_elevated_load=True,
    )


def test_elevated_load_rejects_hundredfold_profile() -> None:
    with pytest.raises(ValueError, match="cannot exceed 40"):
        validate_load_limits(
            maximum_standard_rps=400.0,
            maximum_thorough_rps=None,
            allow_elevated_load=True,
        )


def test_tenfold_rate_plan_has_progressive_stages() -> None:
    assert build_rate_plan(40.0, candidates=_MODULE._STANDARD_RATE_CANDIDATES) == (
        0.5,
        1.0,
        2.0,
        4.0,
        8.0,
        12.0,
        20.0,
        30.0,
        40.0,
    )


def test_percentile_uses_nearest_rank() -> None:
    assert percentile([0.1, 0.2, 0.3, 0.4], 0.95) == 0.4


def test_stage_stops_on_server_error() -> None:
    summary = StageSummary(
        strength="standard",
        target_rps=1.0,
        duration_seconds=10,
        results=[RequestResult(status=503, duration_seconds=0.2, degraded=False)],
    )

    assert evaluate_stage(summary, p95_limit_seconds=10.0) == "server error observed"


def test_stage_stops_when_p95_exceeds_limit() -> None:
    summary = StageSummary(
        strength="standard",
        target_rps=1.0,
        duration_seconds=10,
        results=[RequestResult(status=200, duration_seconds=11.0, degraded=False)],
    )

    assert evaluate_stage(summary, p95_limit_seconds=10.0) == "p95 latency exceeded 10.00s"


def test_report_writes_machine_readable_and_visual_artifacts(tmp_path: Path) -> None:
    summary = StageSummary(
        strength="standard",
        target_rps=1.0,
        duration_seconds=10,
        results=[
            RequestResult(
                status=200,
                duration_seconds=1.25,
                degraded=False,
                started_offset_seconds=0.0,
            )
        ],
        wall_seconds=10.0,
    )

    artifacts = write_report(
        tmp_path,
        base_url="https://scholight.example.com",
        started_at=datetime(2026, 7, 25, tzinfo=UTC),
        finished_at=datetime(2026, 7, 25, 0, 0, 10, tzinfo=UTC),
        stages=[summary],
    )

    assert {path.name for path in artifacts} == {
        "latency-by-stage.svg",
        "outcomes-by-stage.svg",
        "report.html",
        "request-timeline.svg",
        "requests.csv",
        "results.json",
    }


def test_report_json_excludes_credentials(tmp_path: Path) -> None:
    write_report(
        tmp_path,
        base_url="https://scholight.example.com",
        started_at=datetime(2026, 7, 25, tzinfo=UTC),
        finished_at=datetime(2026, 7, 25, 0, 0, 1, tzinfo=UTC),
        stages=[],
    )

    payload = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))

    assert set(payload) == {"base_url", "finished_at", "stages", "started_at"}


def test_report_html_contains_all_three_charts(tmp_path: Path) -> None:
    write_report(
        tmp_path,
        base_url="https://scholight.example.com",
        started_at=datetime(2026, 7, 25, tzinfo=UTC),
        finished_at=datetime(2026, 7, 25, 0, 0, 1, tzinfo=UTC),
        stages=[],
    )

    report = (tmp_path / "report.html").read_text(encoding="utf-8")

    assert report.count("<img ") == 3
