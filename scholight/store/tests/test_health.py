"""Unit tests for scholight.store.health — Zilliz Cloud health checker (4 resource flags)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from scholight.store.health import (
    CheckResult,
    HealthChecker,
    HealthReport,
    HealthStatus,
    LayerResult,
    _cursor_scan,
    _extract_year,
    run_health_check,
)

# ── Helpers ────────────────────────────────────────────────────────────────────


def _mock_client(**overrides: Any) -> MagicMock:
    client = MagicMock()
    client.list_collections.return_value = ["arxiv_papers", "arxiv_chunks"]
    client.has_collection.return_value = True
    client.get_server_version.return_value = {"version": "2.6.7", "deploy_mode": "standalone"}
    client.describe_collection.return_value = {
        "fields": [{"name": "arxiv_id"}, {"name": "title"}],
        "num_shards": 2,
    }
    client.get_load_state.return_value = {"state": "LoadState.Loaded"}
    client.list_indexes.return_value = ["vector_idx"]
    client.describe_index.return_value = {
        "state": "Finished",
        "total_rows": 1000,
        "indexed_rows": 1000,
        "pending_index_rows": 0,
    }
    client.list_loaded_segments.return_value = []
    client.list_persistent_segments.return_value = []
    client.get_collection_stats.return_value = {"row_count": 500}
    client.query.return_value = []
    for k, v in overrides.items():
        setattr(client, k, v)
    return client


# ── Dataclasses (constructed correctly with keyword args) ──────────────────────


class TestCheckResult:
    def test_default_values(self) -> None:
        cr = CheckResult(name="test_check", status=HealthStatus.PASS)
        assert cr.name == "test_check"
        assert cr.status == HealthStatus.PASS
        assert cr.detail == ""
        assert cr.data == {}

    def test_with_detail_and_data(self) -> None:
        cr = CheckResult(
            name="conn", status=HealthStatus.FAIL, detail="timeout", data={"latency": 30}
        )
        assert cr.detail == "timeout"
        assert cr.data == {"latency": 30}

    def test_all_statuses(self) -> None:
        for s in HealthStatus:
            cr = CheckResult(name="x", status=s)
            assert cr.status == s


class TestLayerResult:
    def test_empty_layer_passes(self) -> None:
        layer = LayerResult(layer="test")
        assert layer.worst == HealthStatus.PASS
        assert layer.pass_count == 0
        assert layer.warn_count == 0
        assert layer.fail_count == 0

    def test_worst_is_fail_when_mixed(self) -> None:
        layer = LayerResult(layer="test")
        layer.checks.append(CheckResult(name="a", status=HealthStatus.PASS))
        layer.checks.append(CheckResult(name="b", status=HealthStatus.WARN))
        layer.checks.append(CheckResult(name="c", status=HealthStatus.FAIL))
        assert layer.worst == HealthStatus.FAIL
        assert layer.pass_count == 1
        assert layer.warn_count == 1
        assert layer.fail_count == 1

    def test_worst_is_warn_without_fail(self) -> None:
        layer = LayerResult(layer="test")
        layer.checks.append(CheckResult(name="a", status=HealthStatus.PASS))
        layer.checks.append(CheckResult(name="b", status=HealthStatus.WARN))
        assert layer.worst == HealthStatus.WARN
        assert layer.pass_count == 1
        assert layer.warn_count == 1

    def test_all_pass(self) -> None:
        layer = LayerResult(layer="test")
        layer.checks.append(CheckResult(name="a", status=HealthStatus.PASS))
        layer.checks.append(CheckResult(name="b", status=HealthStatus.PASS))
        assert layer.worst == HealthStatus.PASS
        assert layer.pass_count == 2
        assert layer.fail_count == 0

    def test_duration_default(self) -> None:
        layer = LayerResult(layer="test")
        assert layer.duration_ms == 0.0


class TestHealthReport:
    def test_empty_report(self) -> None:
        report = HealthReport()
        assert report.summary == {"pass": 0, "warn": 0, "fail": 0}
        assert report.healthy is True
        assert report.deep is False

    def test_summary_aggregates_all_layers(self) -> None:
        report = HealthReport()
        l1 = LayerResult(layer="L0")
        l1.checks = [
            CheckResult(name="a", status=HealthStatus.PASS),
            CheckResult(name="b", status=HealthStatus.WARN),
        ]
        l2 = LayerResult(layer="L1")
        l2.checks = [
            CheckResult(name="c", status=HealthStatus.FAIL),
            CheckResult(name="d", status=HealthStatus.PASS),
        ]
        report.layers = [l1, l2]
        assert report.summary == {"pass": 2, "warn": 1, "fail": 1}
        assert report.healthy is False

    def test_healthy_when_no_fails(self) -> None:
        report = HealthReport()
        layer = LayerResult(layer="L0")
        layer.checks = [CheckResult(name="a", status=HealthStatus.PASS)]
        report.layers = [layer]
        assert report.healthy is True

    def test_to_dict_structure(self) -> None:
        report = HealthReport(deep=True)
        report.total_duration_ms = 150.0
        report.analyzed_at = "2024-01-01T00:00:00Z"
        layer = LayerResult(layer="L0 Connection", duration_ms=10.0)
        layer.checks = [
            CheckResult(name="reachability", status=HealthStatus.PASS, detail="Connected"),
        ]
        report.layers = [layer]

        d = report.to_dict()
        assert d["deep"] is True
        assert d["healthy"] is True
        assert d["total_duration_ms"] == 150.0
        assert d["analyzed_at"] == "2024-01-01T00:00:00Z"
        assert d["summary"] == {"pass": 1, "warn": 0, "fail": 0}
        assert len(d["layers"]) == 1
        assert d["layers"][0]["layer"] == "L0 Connection"
        assert d["layers"][0]["status"] == "pass"
        assert d["layers"][0]["checks"][0]["name"] == "reachability"

    def test_print_healthy_report(self) -> None:
        report = HealthReport()
        report.analyzed_at = "2024-01-01T00:00:00Z"
        report.total_duration_ms = 100.0
        layer = LayerResult(layer="L0 Connection", duration_ms=10.0)
        layer.checks = [
            CheckResult(name="reachability", status=HealthStatus.PASS, detail="Connected")
        ]
        report.layers = [layer]

        output = report.print()
        assert "Health Report (QUICK)" in output
        assert "PASS=1  WARN=0  FAIL=0" in output
        assert "[✓] L0 Connection" in output
        assert "✓ reachability" in output
        assert "All checks passed" in output

    def test_print_unhealthy_report(self) -> None:
        report = HealthReport(deep=True)
        report.analyzed_at = "2024-01-01T00:00:00Z"
        report.total_duration_ms = 100.0
        layer = LayerResult(layer="L0 Connection", duration_ms=10.0)
        layer.checks = [CheckResult(name="reachability", status=HealthStatus.FAIL, detail="down")]
        report.layers = [layer]

        output = report.print()
        assert "Health Report (DEEP)" in output
        assert "PASS=0  WARN=0  FAIL=1" in output
        assert "[✗] L0 Connection" in output
        assert "✗ reachability" in output
        assert "check(s) failed" in output

    def test_print_renders_detail_lines(self) -> None:
        report = HealthReport()
        report.analyzed_at = "2024-01-01T00:00:00Z"
        report.total_duration_ms = 100.0
        layer = LayerResult(layer="L0", duration_ms=10.0)
        layer.checks = [CheckResult(name="test", status=HealthStatus.PASS, detail="line1\nline2")]
        report.layers = [layer]

        output = report.print()
        assert "line1" in output
        assert "line2" in output

    def test_print_warn_status(self) -> None:
        report = HealthReport()
        report.analyzed_at = "2024-01-01T00:00:00Z"
        report.total_duration_ms = 100.0
        layer = LayerResult(layer="L0", duration_ms=10.0)
        layer.checks = [CheckResult(name="w", status=HealthStatus.WARN)]
        report.layers = [layer]

        output = report.print()
        assert "[⚠] L0" in output
        assert "⚠ w" in output


# ── _extract_year ──────────────────────────────────────────────────────────────


class TestExtractYear:
    def test_new_style_2024(self) -> None:
        assert _extract_year("2401.12345") == 2024

    def test_new_style_2099(self) -> None:
        assert _extract_year("9912.54321") == 2099

    def test_old_style_pre_2000(self) -> None:
        assert _extract_year("hep-th/9901001") == 1999

    def test_old_style_post_2000(self) -> None:
        assert _extract_year("cs.AI/0704123") == 2007

    def test_old_style_boundary_91(self) -> None:
        assert _extract_year("cond-mat/9101001") == 1991

    def test_old_style_boundary_90(self) -> None:
        assert _extract_year("hep-ph/9001001") == 2090

    def test_new_style_year_2000(self) -> None:
        assert _extract_year("0001.00001") == 2000


# ── _cursor_scan ───────────────────────────────────────────────────────────────


class TestCursorScan:
    def test_single_batch(self) -> None:
        client = MagicMock()
        client.query.side_effect = [
            [{"arxiv_id": "2401.00001", "title": "A"}, {"arxiv_id": "2401.00002", "title": "B"}],
            [],
        ]
        results = _cursor_scan(client, "papers", ["arxiv_id", "title"])
        assert len(results) == 2
        assert client.query.call_count == 2

    def test_multiple_batches(self) -> None:
        client = MagicMock()
        client.query.side_effect = [
            [{"arxiv_id": "2401.00001"}, {"arxiv_id": "2401.00002"}],
            [{"arxiv_id": "2401.00003"}],
            [],
        ]
        results = _cursor_scan(client, "papers", ["arxiv_id"], limit=2)
        assert len(results) == 3
        assert client.query.call_count == 3

    def test_empty_collection(self) -> None:
        client = MagicMock()
        client.query.return_value = []
        results = _cursor_scan(client, "papers", ["arxiv_id"])
        assert results == []

    def test_stop_after(self) -> None:
        client = MagicMock()
        client.query.side_effect = [
            [{"arxiv_id": "2401.00001"}, {"arxiv_id": "2401.00002"}],
            [{"arxiv_id": "2401.00003"}, {"arxiv_id": "2401.00004"}],
            [],
        ]
        results = _cursor_scan(client, "papers", ["arxiv_id"], limit=2, stop_after=3)
        assert len(results) == 3
        assert client.query.call_count == 2

    def test_custom_pk(self) -> None:
        client = MagicMock()
        client.query.side_effect = [[{"chunk_id": "c1"}], []]
        results = _cursor_scan(client, "chunks", ["chunk_id"], pk="chunk_id")
        assert len(results) == 1


# ── L0: Connection ─────────────────────────────────────────────────────────────


def _detail_of(layer: LayerResult, name_contains: str) -> str:
    for c in layer.checks:
        if name_contains in c.name:
            return c.detail
    return ""


def _has_check_with_status_name(layer: LayerResult, keyword: str, detail_substr: str = "") -> bool:
    for c in layer.checks:
        s = c.name
        if keyword in s and (not detail_substr or detail_substr in c.detail):
            return True
    return False


class TestCheckConnection:
    def test_pass_connected(self) -> None:
        mock = _mock_client()
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_connection()

        assert len(result.checks) == 2
        assert _has_check_with_status_name(result, "reachability")
        assert _has_check_with_status_name(result, "server_version")

    def test_fail_unreachable(self) -> None:
        mock = _mock_client()
        mock.list_collections.side_effect = RuntimeError("connection refused")
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_connection()

        assert len(result.checks) == 1
        assert "connection refused" in result.checks[0].detail

    def test_warn_version_unavailable(self) -> None:
        mock = _mock_client()
        mock.get_server_version.side_effect = RuntimeError("no version")
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_connection()

        assert len(result.checks) == 2
        assert _has_check_with_status_name(result, "reachability")
        assert "no version" in _detail_of(result, "server_version")

    def test_version_non_dict(self) -> None:
        mock = _mock_client()
        mock.get_server_version.return_value = "v2.6.7"
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_connection()

        assert len(result.checks) == 2

    def test_version_no_hasattr(self) -> None:
        mock = _mock_client()
        del mock.get_server_version
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_connection()

        assert len(result.checks) == 2
        assert result.checks[1].data["raw"] == {}


# ── L1: Collections ────────────────────────────────────────────────────────────


class TestCheckCollections:
    def test_all_collections_healthy(self) -> None:
        mock = _mock_client()
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_collections()

        assert len(result.checks) >= 4

    def test_collection_missing(self) -> None:
        mock = _mock_client()
        mock.has_collection.side_effect = lambda name: name != "arxiv_papers"
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_collections()

        assert any("/exists" in c.name for c in result.checks)
        assert any("arxiv_chunks" in c.name for c in result.checks)

    def test_schema_describe_failure(self) -> None:
        mock = _mock_client()
        mock.describe_collection.side_effect = RuntimeError("schema error")
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_collections()

        assert any("schema" in c.name for c in result.checks)
        assert any("schema error" in c.detail for c in result.checks)

    def test_collection_not_loaded_no_fix(self) -> None:
        mock = _mock_client()
        mock.get_load_state.return_value = {"state": "NotLoad"}
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_collections()

        not_loaded = [c for c in result.checks if "loaded" in c.name]
        assert len(not_loaded) >= 2
        assert all("--fix" in c.detail for c in not_loaded)

    def test_collection_loading(self) -> None:
        mock = _mock_client()
        mock.get_load_state.return_value = {"state": "LoadState.Loading"}
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_collections()

        loading = [c for c in result.checks if "loaded" in c.name]
        assert len(loading) >= 2
        assert any("Still loading" in c.detail for c in loading)

    def test_fix_auto_load_success(self) -> None:
        mock = _mock_client()
        mock.get_load_state.return_value = {"state": "NotLoad"}
        mock.load_collection.return_value = None
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker(fix=True)
            result = checker.check_collections()

        assert mock.load_collection.call_count == 2
        loaded = [c for c in result.checks if "loaded" in c.name]
        assert len(loaded) >= 2
        assert all("auto-loaded" in c.detail for c in loaded)

    def test_fix_auto_load_failure(self) -> None:
        mock = _mock_client()
        mock.get_load_state.return_value = {"state": "NotLoad"}
        mock.load_collection.side_effect = RuntimeError("load timeout")
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker(fix=True)
            result = checker.check_collections()

        failed = [c for c in result.checks if "loaded" in c.name and "load failed" in c.detail]
        assert len(failed) >= 2

    def test_load_state_query_fails(self) -> None:
        mock = _mock_client()
        mock.get_load_state.side_effect = RuntimeError("state error")
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_collections()

        assert any(
            "loaded" in c.name and "Cannot check load state" in c.detail for c in result.checks
        )


# ── L2: Indexes ────────────────────────────────────────────────────────────────


class TestCheckIndexes:
    def test_indexes_healthy(self) -> None:
        mock = _mock_client()
        mock.list_indexes.return_value = ["dense_vec", "sparse_vec"]
        mock.describe_index.return_value = {
            "state": "Finished",
            "total_rows": 1000,
            "indexed_rows": 1000,
            "pending_index_rows": 0,
        }
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_indexes()

        assert len(result.checks) >= 4

    def test_no_indexes(self) -> None:
        mock = _mock_client()
        mock.list_indexes.return_value = []
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_indexes()

        assert any("No indexes" in c.detail for c in result.checks)

    def test_index_pending(self) -> None:
        mock = _mock_client()
        mock.list_indexes.return_value = ["dense_vec"]
        mock.describe_index.return_value = {
            "state": "InProgress",
            "total_rows": 1000,
            "indexed_rows": 500,
            "pending_index_rows": 500,
        }
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_indexes()

        assert any("indexing in progress" in c.detail for c in result.checks)

    def test_index_unknown_state(self) -> None:
        mock = _mock_client()
        mock.list_indexes.return_value = ["dense_vec"]
        mock.describe_index.return_value = {
            "state": "Unknown",
            "total_rows": 1000,
            "indexed_rows": 0,
            "pending_index_rows": 0,
        }
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_indexes()

        assert len(result.checks) > 0

    def test_list_indexes_failure(self) -> None:
        mock = _mock_client()
        mock.list_indexes.side_effect = RuntimeError("list error")
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_indexes()

        assert any("list_indexes failed" in c.detail for c in result.checks)

    def test_describe_index_failure(self) -> None:
        mock = _mock_client()
        mock.list_indexes.return_value = ["dense_vec"]
        mock.describe_index.side_effect = RuntimeError("describe error")
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_indexes()

        assert any("describe failed" in c.detail for c in result.checks)

    def test_collection_missing_skipped(self) -> None:
        mock = _mock_client()
        mock.has_collection.return_value = False
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_indexes()

        assert not result.checks

    def test_index_state_integer_3(self) -> None:
        mock = _mock_client()
        mock.list_indexes.return_value = ["dense_vec"]
        mock.describe_index.return_value = {
            "state": 3,
            "total_rows": 1000,
            "indexed_rows": 1000,
            "pending_index_rows": 0,
        }
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_indexes()

        assert len(result.checks) > 0
        assert any("1000/1000 rows indexed" in c.detail for c in result.checks)

    def test_index_summary(self) -> None:
        mock = _mock_client()
        mock.list_indexes.return_value = ["dense_vec", "sparse_vec"]
        mock.describe_index.return_value = {
            "state": "Finished",
            "total_rows": 1000,
            "indexed_rows": 1000,
            "pending_index_rows": 0,
        }
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_indexes()

        summary = [c for c in result.checks if "index_summary" in c.name]
        assert len(summary) >= 2
        assert any("2 index(es) OK" in s.detail for s in summary)


# ── L3: Segments ───────────────────────────────────────────────────────────────


class TestCheckSegments:
    def test_segments_healthy(self) -> None:
        mock = _mock_client()
        seg = MagicMock()
        seg.num_rows = 100
        seg.mem_size = 1024
        seg.level = 0
        mock.list_loaded_segments.return_value = [seg, seg]
        mock.list_persistent_segments.return_value = [seg]
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_segments()

        assert len(result.checks) >= 2
        assert any("segments" in c.name for c in result.checks)

    def test_no_segments_returns_check(self) -> None:
        mock = _mock_client()
        mock.list_loaded_segments.return_value = []
        mock.list_persistent_segments.return_value = []
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_segments()

        assert len(result.checks) >= 2
        assert all("0 loaded, 0 persistent" in c.detail for c in result.checks)

    def test_loaded_segments_failure(self) -> None:
        mock = _mock_client()
        mock.list_loaded_segments.side_effect = RuntimeError("load seg error")
        mock.list_persistent_segments.return_value = []
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_segments()

        assert any("list_loaded_segments failed" in c.detail for c in result.checks)

    def test_persistent_segments_failure(self) -> None:
        mock = _mock_client()
        mock.list_loaded_segments.return_value = []
        mock.list_persistent_segments.side_effect = RuntimeError("persistent seg error")
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_segments()

        assert any("list_persistent_segments failed" in c.detail for c in result.checks)

    def test_collection_missing_skipped(self) -> None:
        mock = _mock_client()
        mock.has_collection.return_value = False
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_segments()

        assert not result.checks


# ── L4: Data Stats ─────────────────────────────────────────────────────────────


class TestCheckDataStats:
    def test_quick_mode_row_count(self) -> None:
        mock = _mock_client()
        mock.get_collection_stats.return_value = {"row_count": 1234}
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker(deep=False)
            result = checker.check_data_stats()

        assert any("1,234 rows" in c.detail for c in result.checks)
        assert any("quick mode" in c.detail for c in result.checks)

    def test_stats_failure(self) -> None:
        mock = _mock_client()
        mock.get_collection_stats.side_effect = RuntimeError("stats error")
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_data_stats()

        assert any("stats failed" in c.detail for c in result.checks)

    def test_deep_mode_skip_when_missing_collection(self) -> None:
        mock = _mock_client()
        mock.has_collection.side_effect = lambda name: name != "arxiv_papers"
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker(deep=True)
            result = checker.check_data_stats()

        assert not any("arxiv_papers/scan" in c.name for c in result.checks)

    def test_deep_mode_year_distribution(self) -> None:
        mock = _mock_client()
        mock.get_collection_stats.return_value = {"row_count": 3}
        mock.query.side_effect = [
            [
                {"arxiv_id": "2401.00001"},
                {"arxiv_id": "2401.00002"},
                {"arxiv_id": "2401.00003"},
            ],
            [],
            [
                {
                    "arxiv_id": "2401.00001",
                    "title": "P1",
                    "authors": ["Alice"],
                    "abstract": "test",
                    "categories": ["cs.AI"],
                    "created": "2024-01-01",
                    "updated": "2024-06-01",
                    "license": "CC-BY",
                    "doi": "10.1234",
                    "journal_ref": "Nature",
                    "comments": "",
                    "acm_class": "",
                },
            ],
            [],
        ]
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker(deep=True)
            result = checker.check_data_stats()

        years_check = [c for c in result.checks if "years" in c.name]
        assert len(years_check) == 1
        assert years_check[0].status == HealthStatus.PASS
        assert "2024" in years_check[0].detail

    def test_deep_mode_scan_failure(self) -> None:
        mock = _mock_client()
        mock.get_collection_stats.return_value = {"row_count": 100}
        mock.query.side_effect = RuntimeError("scan error")
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker(deep=True)
            result = checker.check_data_stats()

        assert any("Cursor scan failed" in c.detail for c in result.checks)

    def test_deep_mode_field_completeness(self) -> None:
        mock = _mock_client()
        mock.get_collection_stats.return_value = {"row_count": 2}
        mock.query.side_effect = [
            [{"arxiv_id": "2401.00001"}, {"arxiv_id": "2401.00002"}],
            [],
            [
                {
                    "arxiv_id": "2401.00001",
                    "title": "Paper 1",
                    "authors": ["Alice"],
                    "abstract": "",
                    "categories": ["cs.AI"],
                    "created": "2024-01-01",
                    "updated": "2024-06-01",
                    "license": "CC-BY",
                    "doi": "",
                    "journal_ref": "",
                    "comments": "",
                    "acm_class": "",
                },
                {
                    "arxiv_id": "2401.00002",
                    "title": "Paper 2",
                    "authors": [],
                    "abstract": "Some text",
                    "categories": [],
                    "created": "2024-01-02",
                    "updated": "2024-06-02",
                    "license": "",
                    "doi": "",
                    "journal_ref": "",
                    "comments": "",
                    "acm_class": "",
                },
            ],
            [],
        ]
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker(deep=True)
            result = checker.check_data_stats()

        fields_check = [c for c in result.checks if "fields" in c.name]
        assert len(fields_check) == 1


# ── L5: Resources (4 flags) ────────────────────────────────────────────────────


class TestCheckResources:
    def test_quick_mode_all_flags_ok(self) -> None:
        mock = _mock_client()
        mock.query.return_value = []
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker(deep=False)
            result = checker.check_resources()

        assert len(result.checks) == 4
        assert all("All papers have" in c.detail for c in result.checks)

    def test_quick_mode_missing_flag(self) -> None:
        mock = _mock_client()
        mock.query.return_value = [{"arxiv_id": "2401.00001"}]
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker(deep=False)
            result = checker.check_resources()

        assert len(result.checks) == 4
        assert all("Papers with" in c.detail for c in result.checks)

    def test_query_failure_flag(self) -> None:
        mock = _mock_client()
        mock.query.side_effect = RuntimeError("query error")
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker(deep=False)
            result = checker.check_resources()

        assert any("Query failed" in c.detail for c in result.checks)

    def test_missing_collection_returns_empty(self) -> None:
        mock = _mock_client()
        mock.has_collection.return_value = False
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_resources()

        assert not result.checks

    def test_deep_mode_counts_missing(self) -> None:
        mock = _mock_client()

        # 4 quick queries → all flags ok, then deep cursor scans for 4 flags
        quick_responses = [[], [], [], []]
        deep_responses = [
            [
                {"arxiv_id": "2401.00001", "has_latex": False},
                {"arxiv_id": "2401.00002", "has_latex": True},
            ],
            [],
            [{"arxiv_id": "2401.00001", "has_pdf": True}],
            [],
            [],
            [{"arxiv_id": "2401.00001", "has_chunks": False}],
            [],
        ]
        mock.query.side_effect = quick_responses + deep_responses
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker(deep=True)
            result = checker.check_resources()

        missing_counts = [c for c in result.checks if "_count" in c.name]
        assert len(missing_counts) > 0

    def test_deep_mode_scan_failure(self) -> None:
        mock = _mock_client()
        quick_responses = [[], [], [], []]
        mock.query.side_effect = [*quick_responses, RuntimeError("deep scan error")]
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker(deep=True)
            result = checker.check_resources()

        assert any("Deep scan failed" in c.detail for c in result.checks)


# ── L6: Vectors ────────────────────────────────────────────────────────────────


class TestCheckVectors:
    def test_quick_mode_all_nonzero(self) -> None:
        mock = _mock_client()
        mock.query.return_value = [
            {"arxiv_id": "2401.00001", "abstract_embedding": [0.1, 0.2]},
            {"arxiv_id": "2401.00002", "abstract_embedding": [0.3, 0.4]},
        ]
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker(deep=False)
            result = checker.check_vectors()

        assert any("non-zero" in c.detail for c in result.checks)

    def test_quick_mode_zero_vectors(self) -> None:
        mock = _mock_client()
        mock.query.return_value = [
            {"arxiv_id": "2401.00001", "abstract_embedding": [0.0, 0.0]},
            {"arxiv_id": "2401.00002", "abstract_embedding": [0.1, 0.2]},
        ]
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker(deep=False)
            result = checker.check_vectors()

        assert any("zero vectors" in c.detail for c in result.checks)

    def test_quick_mode_query_failure(self) -> None:
        mock = _mock_client()
        mock.query.side_effect = RuntimeError("query error")
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker(deep=False)
            result = checker.check_vectors()

        assert any("Check failed" in c.detail for c in result.checks)

    def test_deep_mode_uses_cursor_scan(self) -> None:
        mock = _mock_client()
        mock.query.side_effect = [
            [{"arxiv_id": "2401.00001", "abstract_embedding": [0.1, 0.2]}],
            [],
            [{"chunk_id": "c1", "content_embedding": [0.3, 0.4]}],
            [],
        ]
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker(deep=True)
            result = checker.check_vectors()

        assert mock.query.call_count > 0
        assert len(result.checks) > 0

    def test_deep_mode_zero_vectors(self) -> None:
        mock = _mock_client()
        mock.query.side_effect = [
            [{"arxiv_id": "2401.00001", "abstract_embedding": [0.0, 0.0]}],
            [],
            [{"chunk_id": "c1", "content_embedding": [0.0, 0.0]}],
            [],
        ]
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker(deep=True)
            result = checker.check_vectors()

        assert any("zero vectors" in c.detail for c in result.checks)

    def test_deep_mode_scan_failure(self) -> None:
        mock = _mock_client()
        mock.query.side_effect = RuntimeError("deep scan error")
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker(deep=True)
            result = checker.check_vectors()

        assert any("Deep scan failed" in c.detail for c in result.checks)

    def test_missing_collection_skipped(self) -> None:
        mock = _mock_client()
        mock.has_collection.return_value = False
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_vectors()

        assert not result.checks


# ── L7: Consistency ────────────────────────────────────────────────────────────


class TestCheckConsistency:
    def test_both_collections_healthy(self) -> None:
        mock = _mock_client()
        mock.get_collection_stats.side_effect = [{"row_count": 500}, {"row_count": 3000}]
        mock.query.side_effect = [
            [{"arxiv_id": "2401.00001"}],
            [{"chunk_id": "c1"}],
            [],
        ]
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_consistency()

        assert any("row_counts" in c.name for c in result.checks)
        assert any("chunk_consistency" in c.name for c in result.checks)

    def test_missing_collection_warns(self) -> None:
        mock = _mock_client()
        mock.has_collection.side_effect = lambda name: name != "arxiv_chunks"
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_consistency()

        assert any("Both collections required" in c.detail for c in result.checks)

    def test_row_count_stats_failure(self) -> None:
        mock = _mock_client()
        mock.get_collection_stats.side_effect = RuntimeError("stats fail")
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_consistency()

        assert any("row_counts" in c.name and "Stats failed" in c.detail for c in result.checks)

    def test_chunk_consistency_mismatch(self) -> None:
        mock = _mock_client()
        mock.get_collection_stats.side_effect = [{"row_count": 500}, {"row_count": 3000}]
        mock.query.side_effect = [
            [{"arxiv_id": "2401.00001"}],
            [],
            [],
        ]
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_consistency()

        consistency = [c for c in result.checks if "chunk_consistency" in c.name]
        assert len(consistency) == 1
        assert "no chunks found" in consistency[0].detail.lower()

    def test_chunk_consistency_query_failure(self) -> None:
        mock = _mock_client()
        mock.get_collection_stats.side_effect = [{"row_count": 500}, {"row_count": 3000}]
        mock.query.side_effect = RuntimeError("consistency query error")
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            result = checker.check_consistency()

        assert any(
            "chunk_consistency" in c.name and "Check failed" in c.detail for c in result.checks
        )


# ── HealthChecker.run() ────────────────────────────────────────────────────────


class TestHealthCheckerRun:
    def test_run_all_layers_default(self) -> None:
        mock = _mock_client()
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            report = checker.run()

        assert len(report.layers) == 8
        assert report.layers[0].layer == "L0 Connection"
        assert report.layers[7].layer == "L7 Consistency"
        assert report.deep is False
        assert report.total_duration_ms > 0

    def test_run_with_dims_filter(self) -> None:
        mock = _mock_client()
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker(dims=["connection", "indexes"])
            report = checker.run()

        layer_names = [layer.layer for layer in report.layers]
        assert layer_names == ["L0 Connection", "L2 Indexes"]
        assert len(report.layers) == 2

    def test_run_with_deep_flag(self) -> None:
        mock = _mock_client()
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker(deep=True)
            report = checker.run()

        assert report.deep is True

    def test_run_handles_layer_exception(self) -> None:
        mock = _mock_client()
        mock.has_collection.side_effect = RuntimeError("boom")
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            report = checker.run()

        assert len(report.layers) == 8
        assert any("exception" in check.name for layer in report.layers for check in layer.checks)

    def test_run_reports_analyzed_at(self) -> None:
        mock = _mock_client()
        with patch("scholight.store.health.get_client", return_value=mock):
            checker = HealthChecker()
            report = checker.run()

        assert report.analyzed_at.endswith("Z")
        assert "T" in report.analyzed_at


# ── run_health_check ───────────────────────────────────────────────────────────


class TestRunHealthCheck:
    def test_convenience_function(self) -> None:
        mock = _mock_client()
        with patch("scholight.store.health.get_client", return_value=mock):
            report = run_health_check(deep=False)
            assert isinstance(report, HealthReport)
            assert report.deep is False
            assert len(report.layers) == 8

    def test_convenience_with_dims(self) -> None:
        mock = _mock_client()
        with patch("scholight.store.health.get_client", return_value=mock):
            report = run_health_check(deep=False, dims=["connection"])
            assert len(report.layers) == 1
            assert report.layers[0].layer == "L0 Connection"

    def test_convenience_with_fix(self) -> None:
        mock = _mock_client()
        mock.get_load_state.return_value = {"state": "NotLoad"}
        mock.load_collection.return_value = None
        with patch("scholight.store.health.get_client", return_value=mock):
            report = run_health_check(fix=True)

        assert isinstance(report, HealthReport)
        mock.load_collection.assert_called()
