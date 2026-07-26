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
build_stage_specs = _MODULE.build_stage_specs
evaluate_stage = _MODULE.evaluate_stage
classify_saturation = _MODULE.classify_saturation
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
            maximum_standard_rps=20.0,
            maximum_thorough_rps=None,
            allow_elevated_load=False,
        )


def test_elevated_load_allows_full_bounded_profile() -> None:
    validate_load_limits(
        maximum_standard_rps=20.0,
        maximum_thorough_rps=4.0,
        allow_elevated_load=True,
    )


def test_extreme_load_requires_separate_confirmation() -> None:
    with pytest.raises(ValueError, match="--allow-extreme-load"):
        validate_load_limits(
            maximum_standard_rps=200.0,
            maximum_thorough_rps=40.0,
            allow_elevated_load=True,
        )


def test_extreme_load_still_requires_elevated_confirmation() -> None:
    with pytest.raises(ValueError, match="--allow-elevated-load"):
        validate_load_limits(
            maximum_standard_rps=200.0,
            maximum_thorough_rps=40.0,
            allow_elevated_load=False,
            allow_extreme_load=True,
        )


def test_extreme_load_allows_explicitly_confirmed_profile() -> None:
    validate_load_limits(
        maximum_standard_rps=200.0,
        maximum_thorough_rps=40.0,
        allow_elevated_load=True,
        allow_extreme_load=True,
    )


def test_extreme_load_rejects_above_hard_limit() -> None:
    with pytest.raises(ValueError, match="cannot exceed 200"):
        validate_load_limits(
            maximum_standard_rps=201.0,
            maximum_thorough_rps=40.0,
            allow_elevated_load=True,
            allow_extreme_load=True,
        )


def test_full_rate_plan_has_progressive_stages() -> None:
    assert build_rate_plan(20.0, candidates=_MODULE._STANDARD_RATE_CANDIDATES) == (
        1.0,
        2.0,
        4.0,
        8.0,
        12.0,
        16.0,
        20.0,
    )


def test_extreme_rate_plan_reaches_200_rps_progressively() -> None:
    assert build_rate_plan(200.0, candidates=_MODULE._STANDARD_RATE_CANDIDATES) == (
        1.0,
        2.0,
        4.0,
        8.0,
        12.0,
        16.0,
        20.0,
        40.0,
        80.0,
        120.0,
        160.0,
        200.0,
    )


def test_extreme_thorough_rate_plan_reaches_40_rps_progressively() -> None:
    assert build_rate_plan(40.0, candidates=_MODULE._THOROUGH_RATE_CANDIDATES) == (
        0.5,
        1.0,
        2.0,
        3.0,
        4.0,
        8.0,
        16.0,
        24.0,
        32.0,
        40.0,
    )


def test_thorough_only_stage_plan_excludes_standard() -> None:
    assert build_stage_specs(
        selected_strength="thorough",
        maximum_standard_rps=4.0,
        maximum_thorough_rps=2.0,
    ) == [
        ("thorough", 0.5, 90),
        ("thorough", 1.0, 90),
        ("thorough", 2.0, 90),
    ]


def test_thorough_only_stage_plan_requires_a_maximum() -> None:
    with pytest.raises(ValueError, match="--max-thorough-rps"):
        build_stage_specs(
            selected_strength="thorough",
            maximum_standard_rps=4.0,
            maximum_thorough_rps=None,
        )


def test_percentile_uses_nearest_rank() -> None:
    assert percentile([0.1, 0.2, 0.3, 0.4], 0.95) == 0.4


def test_single_server_error_does_not_stop_stage_family() -> None:
    summary = StageSummary(
        strength="standard",
        target_rps=1.0,
        duration_seconds=10,
        results=[
            RequestResult(
                status=502,
                duration_seconds=0.2,
                degraded=False,
                category="unexpected_5xx",
            )
        ],
    )

    assert evaluate_stage(summary, p95_limit_seconds=10.0) is None


def test_stage_does_not_stop_only_because_p95_exceeds_slo() -> None:
    summary = StageSummary(
        strength="standard",
        target_rps=1.0,
        duration_seconds=10,
        results=[RequestResult(status=200, duration_seconds=11.0, degraded=False)],
    )

    assert evaluate_stage(summary, p95_limit_seconds=10.0) is None


def test_stage_stops_after_five_consecutive_critical_failures() -> None:
    summary = StageSummary(
        strength="standard",
        target_rps=8.0,
        duration_seconds=60,
        max_consecutive_critical=5,
    )

    assert "five consecutive" in (evaluate_stage(summary) or "")


def test_generator_drop_is_not_service_saturation() -> None:
    summary = StageSummary(
        strength="standard",
        target_rps=8.0,
        duration_seconds=60,
        generator_dropped=2,
        wall_seconds=60,
    )

    assert classify_saturation([summary], strength="standard") == "generator limited"


def test_capacity_rejection_is_classified_as_overload_protection() -> None:
    summary = StageSummary(
        strength="standard",
        target_rps=8.0,
        duration_seconds=60,
        results=[
            RequestResult(
                status=503,
                duration_seconds=0.05,
                degraded=False,
                category="capacity_rejected",
            )
        ],
        wall_seconds=60,
    )

    assert classify_saturation([summary], strength="standard") == "overload protected"


def test_saturation_requires_two_consecutive_goodput_plateaus_with_service_signal() -> None:
    def stage(rate: float, successes: int, latency: float, errors: int = 0) -> StageSummary:
        return StageSummary(
            strength="standard",
            target_rps=rate,
            duration_seconds=60,
            results=[
                RequestResult(status=200, duration_seconds=latency, degraded=False)
                for _ in range(successes)
            ]
            + [
                RequestResult(
                    status=500,
                    duration_seconds=latency,
                    degraded=False,
                    category="unexpected_5xx",
                )
                for _ in range(errors)
            ],
            wall_seconds=60,
        )

    stages = [
        stage(4, 240, 1.0),
        stage(8, 245, 2.5, 3),
        stage(12, 245, 3.0, 3),
    ]

    assert classify_saturation(stages, strength="standard") == "saturation likely"


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
        "latency-percentiles.svg",
        "outcome-breakdown.svg",
        "report.html",
        "requests.csv",
        "results.json",
        "throughput-and-goodput.svg",
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

    assert set(payload) == {
        "base_url",
        "conclusions",
        "finished_at",
        "stages",
        "started_at",
    }


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
