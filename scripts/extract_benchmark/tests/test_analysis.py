from __future__ import annotations

import json
from copy import deepcopy

from analyze import compare_outputs, latencies, matrix, summarize


def row(index=0):
    return {
        "index": index,
        "case": "article-0",
        "status": 200,
        "quality": True,
        "latency_ms": 10,
        "result": {
            "content": "Complete paragraph with [source](https://example.org).",
            "rendered": False,
            "content_type": "text/html",
            "fetched_at": "2026-09-23T00:00:00Z",
            "source_bytes": 123,
        },
    }


def test_pairwise_quality_does_not_accept_missing_paragraph_when_markers_pass():
    before = row()
    after = deepcopy(before)
    after["result"]["content"] = "Complete paragraph."
    report = compare_outputs([before], [after])
    assert not report["gate"]
    assert report["mismatches"][0]["fields"] == ["content"]


def test_pairwise_quality_compares_schema_and_all_supported_outcomes():
    before = [row(0), row(1)]
    after = deepcopy(before)
    after[0]["result"]["source_bytes"] = "123"
    after[1]["status"] = 503
    report = compare_outputs(before, after)
    assert not report["gate"]
    assert report["schema_mismatches"] == 1
    assert report["supported_case_failures"] == 1


def test_pairwise_quality_ignores_only_time_and_download_accounting():
    before = row()
    after = deepcopy(before)
    after["result"]["fetched_at"] = "2026-09-23T01:00:00Z"
    after["result"]["source_bytes"] = 0
    assert compare_outputs([before], [after])["gate"]


def test_aggregate_reports_content_mime_and_rendered_groups():
    static, rendered = row(), row(1)
    rendered["result"]["rendered"] = True
    report = latencies([static, rendered])
    assert report["mime_render_counts"] == {"text/html|static": 1, "text/html|rendered": 1}


def test_matrix_does_not_accept_partial_client_results(tmp_path):
    item = {"name": "partial", "mode": "cold", "concurrency": 1, "seed": 42, "variant": "baseline"}
    (tmp_path / "plan.json").write_text(json.dumps([item]))
    child = tmp_path / "partial"
    child.mkdir()
    (child / "container-final.json").write_text("[]")
    (child / "manifest.json").write_text(json.dumps({"requests": 2}))
    (child / "requests.jsonl").write_text(json.dumps({**row(), "category": "article"}) + "\n")
    (child / "memory.jsonl").write_text("")
    assert not matrix(tmp_path)["complete"]


def test_standalone_soak_rejects_truncated_request_set_despite_four_hours(tmp_path):
    (tmp_path / "container-final.json").write_text("[]")
    (tmp_path / "manifest.json").write_text(json.dumps({"requests": 2400}))
    (tmp_path / "requests.jsonl").write_text(
        "\n".join(json.dumps({**row(i), "time": 10000, "category": "article"}) for i in range(2000))
    )
    (tmp_path / "memory.jsonl").write_text(
        "\n".join(
            json.dumps({"time": time, "working_set": 1, "oom_kill": 0})
            for time in (0, 601, 12000, 14400)
        )
    )
    assert not summarize(tmp_path)["memory"]["gate"]
