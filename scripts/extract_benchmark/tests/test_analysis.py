from __future__ import annotations

from copy import deepcopy

from analyze import compare_outputs, latencies


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
