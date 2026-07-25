from __future__ import annotations

import importlib.util
import sys
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


def test_rate_plan_stops_at_requested_maximum() -> None:
    assert build_rate_plan(2.0, candidates=(0.5, 1.0, 2.0, 4.0)) == (0.5, 1.0, 2.0)


def test_rate_plan_includes_nonstandard_maximum() -> None:
    assert build_rate_plan(3.0, candidates=(0.5, 1.0, 2.0, 4.0)) == (0.5, 1.0, 2.0, 3.0)


def test_remote_target_requires_explicit_confirmation() -> None:
    with pytest.raises(ValueError, match="--allow-remote"):
        validate_target("https://scholight.example.com", allow_remote=False)


def test_loopback_target_does_not_require_remote_confirmation() -> None:
    assert validate_target("http://127.0.0.1:8000", allow_remote=False) == ("http://127.0.0.1:8000")


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
