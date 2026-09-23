from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from observation_series import aggregate, canary_schedule, coverage, memory_trends


def stamp(seconds):
    return (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds)).isoformat()


def window(root, name, start, end, completions=(), *, complete=True):
    path = root / name
    path.mkdir()
    (path / "window.json").write_text(
        json.dumps(
            {
                "start": stamp(start),
                "end": stamp(end),
                "collected_at": stamp(end + 120),
                "account": "fixture-account",
                "region": "fixture-region",
                "cluster": "fixture-cluster",
                "complete": complete,
            }
        )
    )
    for file in ("completion", "memory", "lifecycle-errors"):
        (path / (file + ".jsonl")).write_text(
            "".join(json.dumps(row) + "\n" for row in completions) if file == "completion" else ""
        )
    (path / "tasks.json").write_text("[]")
    (path / "resources.json").write_text("[]")
    return path


def event(identity, seconds=100):
    return {
        "event_id": identity,
        "stream": "api/fixture",
        "timestamp": stamp(seconds),
        "request_id": identity,
        "scope": "rest",
        "outcome": "initial_success",
        "pagination": False,
        "mime": "html",
        "render_mode": "auto",
        "duration_ms": 10,
    }


def test_overlapping_windows_deduplicate_before_separating_canary_traffic(tmp_path):
    rows = [event("natural"), event("canary")]
    first = window(tmp_path, "first", 0, 200, rows)
    second = window(tmp_path, "second", 50, 300, rows)
    report = aggregate([first, second], start=stamp(0), end=stamp(300), canary_ids={"canary"})
    assert report["completions"]["natural_public_initial"]["requests"] == 1
    assert report["completions"]["canary"]["requests"] == 1
    assert report["deduplicated_events"]["completion"] == 2
    assert not report["memory"]["available"]  # Missing telemetry cannot mean zero memory/OOM.


def test_incomplete_window_cannot_bridge_an_observation_gap(tmp_path):
    first = window(tmp_path, "first", 0, 200)
    missing = window(tmp_path, "missing", 190, 500, complete=False)
    last = window(tmp_path, "last", 400, 600)
    report = aggregate([first, missing, last], start=stamp(0), end=stamp(600), canary_ids=set())
    assert not report["coverage"]["complete"]
    assert report["coverage"]["missing_seconds"] == 200
    assert len(report["incomplete_inputs"]) == 1


def test_coverage_clips_windows_to_selected_release_interval():
    report = coverage([(0, 40), (30, 100)], 10, 90)
    assert report["complete"] and report["covered_seconds"] == 80


def test_idle_trend_excludes_real_internal_work_and_does_not_mix_task_generations():
    mib = 1024**2
    rows = []
    for stream, growth in (("extract/old", 40), ("extract/new", 0)):
        for seconds, value in ((601, 100), (700, 500), (800, 100), (12000, 100 + growth)):
            rows.extend(
                [
                    {
                        "stream": stream,
                        "timestamp": stamp(seconds - 0.01),
                        "ParserActive": 0,
                        "BrowserActive": 0,
                    },
                    {
                        "stream": stream,
                        "timestamp": stamp(seconds),
                        "MemoryWorkingSet": value * mib,
                    },
                ]
            )
        rows.append({"stream": stream, "timestamp": stamp(0), "MemoryWorkingSet": 100 * mib})
    active = {
        "stream": "extract/old",
        "timestamp": stamp(701),
        "duration_ms": 2000,
        "scope": "internal",
    }
    trends = memory_trends(rows, [active])
    assert trends["streams"]["extract/old"]["idle_median_growth_bytes"] == 40 * mib
    assert trends["streams"]["extract/old"]["idle_growth_within_limit"] is False
    assert trends["streams"]["extract/new"]["idle_median_growth_bytes"] == 0
    assert trends["sampling_gap_count"] > 0


def test_oom_task_is_preserved_even_when_later_snapshot_only_has_running_task(tmp_path):
    first = window(tmp_path, "first", 0, 200)
    second = window(tmp_path, "second", 190, 400)
    (first / "tasks.json").write_text(
        json.dumps(
            [
                {
                    "taskArn": "owned-failed-task",
                    "stoppedAt": stamp(150),
                    "stoppedReason": "Essential container in task exited",
                    "containers": [{"exitCode": 137, "reason": "OutOfMemoryError"}],
                }
            ]
        )
    )
    report = aggregate([first, second], start=stamp(0), end=stamp(400), canary_ids=set())
    assert report["tasks"]["oom_task_count"] == 1


def test_gap_between_restarted_tasks_is_visible_in_memory_evidence():
    rows = [
        {"stream": stream, "timestamp": stamp(time), "MemoryWorkingSet": 100}
        for stream, time in (("extract/one", 0), ("extract/one", 1), ("extract/two", 100))
    ]
    report = memory_trends(rows, [])
    assert report["global_sampling_gap_count"] == 1
    assert report["largest_global_sampling_gap_seconds"] == 99


def test_canary_schedule_excludes_login_setup_and_keeps_failed_runtime_runs():
    reports = [
        {"action": "setup", "passed": False, "records": [{"time": stamp(1)}]},
        {"action": "run", "passed": True, "records": [{"time": stamp(100)}]},
        {"action": "run", "passed": False, "records": [{"time": stamp(21700)}]},
    ]
    report = canary_schedule(reports, stamp(0), stamp(43201))
    assert len(report["runs"]) == 2 and report["failed_runs"] == 1
    assert report["six_hour_periods_without_run"] == [2]
