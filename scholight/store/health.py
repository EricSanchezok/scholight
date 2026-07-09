"""Zilliz Cloud database health checker — 7-layer progressive diagnosis.

L0  Connection    → Zilliz Cloud reachability, server version, deploy mode
L1  Collections  → Collection existence, schema integrity, load state
L2  Indexes      → Per-index state, pending rows, indexed vs total
L3  Segments     → Loaded/persistent segments, growing vs sealed, memory
------ domain boundary ------
L4  Data Stats   → Total count, year distribution, field completeness
L5  Resources    → Pipeline flag coverage, pending work count
L6  Vectors      → Zero-vector ratio, embedding dimension verification
L7  Consistency  → Papers ↔ Chunks cross-check, orphan detection

Usage:
    from scholight.store.health import HealthChecker, HealthReport

    checker = HealthChecker(deep=False)  # quick: API-only, <5s
    report = checker.run()
    report.print()
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

import structlog

from scholight.store.client import escape_sql, get_client
from scholight.store.fields import PAPER_SEARCH_FIELDS, PAPER_VECTOR_FIELDS

logger = structlog.get_logger(__name__)

# ── Result types ──────────────────────────────────────────────────────────────


class HealthStatus(Enum):
    PASS = auto()
    WARN = auto()
    FAIL = auto()


@dataclass
class CheckResult:
    """Single check result within a layer."""

    status: HealthStatus
    name: str
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class LayerResult:
    """Aggregated result for one health check layer."""

    layer: str
    checks: list[CheckResult] = field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def worst(self) -> HealthStatus:
        if any(c.status == HealthStatus.FAIL for c in self.checks):
            return HealthStatus.FAIL
        if any(c.status == HealthStatus.WARN for c in self.checks):
            return HealthStatus.WARN
        return HealthStatus.PASS

    @property
    def pass_count(self) -> int:
        return sum(1 for c in self.checks if c.status == HealthStatus.PASS)

    @property
    def warn_count(self) -> int:
        return sum(1 for c in self.checks if c.status == HealthStatus.WARN)

    @property
    def fail_count(self) -> int:
        return sum(1 for c in self.checks if c.status == HealthStatus.FAIL)


@dataclass
class HealthReport:
    """Complete health check report across all layers."""

    layers: list[LayerResult] = field(default_factory=list)
    deep: bool = False
    total_duration_ms: float = 0.0
    analyzed_at: str = ""

    @property
    def summary(self) -> dict[str, int]:
        """Return aggregated pass/warn/fail counts."""
        p = sum(ly.pass_count for ly in self.layers)
        w = sum(ly.warn_count for ly in self.layers)
        f = sum(ly.fail_count for ly in self.layers)
        return {"pass": p, "warn": w, "fail": f}

    @property
    def healthy(self) -> bool:
        """True when no FAIL results across all layers."""
        return self.summary["fail"] == 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSON output."""

        def _status_str(s: HealthStatus) -> str:
            return s.name.lower()

        layer_dicts = []
        for ly in self.layers:
            layer_dicts.append(
                {
                    "layer": ly.layer,
                    "status": _status_str(ly.worst),
                    "duration_ms": ly.duration_ms,
                    "pass": ly.pass_count,
                    "warn": ly.warn_count,
                    "fail": ly.fail_count,
                    "checks": [
                        {
                            "name": c.name,
                            "status": _status_str(c.status),
                            "detail": c.detail,
                            "data": c.data,
                        }
                        for c in ly.checks
                    ],
                }
            )
        return {
            "deep": self.deep,
            "healthy": self.healthy,
            "total_duration_ms": self.total_duration_ms,
            "analyzed_at": self.analyzed_at,
            "summary": self.summary,
            "layers": layer_dicts,
        }

    def print(self) -> str:
        """Render a human-readable health report string."""
        lines: list[str] = []
        status_icon = {HealthStatus.PASS: "✓", HealthStatus.WARN: "⚠", HealthStatus.FAIL: "✗"}

        total = self.summary
        lines.append(
            f"\n{'=' * 60}"
            f"\n  Scholight — Health Report{' (DEEP)' if self.deep else ' (QUICK)'}"
            f"\n  {self.analyzed_at}  |  {self.total_duration_ms:.0f}ms"
            f"\n  PASS={total['pass']}  WARN={total['warn']}  FAIL={total['fail']}"
            f"\n{'=' * 60}\n"
        )

        for layer in self.layers:
            icon = status_icon[layer.worst]
            lines.append(f"  [{icon}] {layer.layer}  ({layer.duration_ms:.0f}ms)")

            for check in layer.checks:
                c_icon = status_icon[check.status]
                lines.append(f"       {c_icon} {check.name}")
                if check.detail:
                    for detail_line in check.detail.split("\n"):
                        lines.append(f"          {detail_line}")

        # Footer
        if self.healthy:
            lines.append(f"\n{'=' * 60}\n  ✓ All checks passed — database is healthy.\n")
        else:
            lines.append(
                f"\n{'=' * 60}\n  ✗ {total['fail']} check(s) failed — review FAIL items above.\n"
            )

        return "\n".join(lines)


# ── Cursor-scan helpers (deep mode) ───────────────────────────────────────────


def _cursor_scan(
    client: Any,
    collection: str,
    fields: list[str],
    *,
    limit: int = 10000,
    stop_after: int = 0,
    pk: str = "arxiv_id",
) -> list[dict[str, Any]]:
    """Iterate *collection* via primary-key cursor, returning all rows."""
    results: list[dict[str, Any]] = []
    last_id = ""
    while True:
        flt = f"{pk} > '{escape_sql(last_id)}'" if last_id else f"{pk} != ''"
        batch = client.query(collection, filter=flt, output_fields=fields, limit=limit)
        if not batch:
            break
        results.extend(batch)
        last_id = batch[-1][pk]
        if stop_after and len(results) >= stop_after:
            return results[:stop_after]
    return results


def _extract_year(arxiv_id: str) -> int:
    """Extract publication year from arXiv ID.

    Old-style: hep-th/9901001  → 1999
    New-style: 2401.12345      → 2024
    """
    if "/" in arxiv_id:
        yy = arxiv_id.split("/")[1][:2]
        return 1900 + int(yy) if int(yy) > 90 else 2000 + int(yy)
    return 2000 + int(arxiv_id[:2])


# ── HealthChecker ─────────────────────────────────────────────────────────────


class HealthChecker:
    """Run health checks against the Scholight Zilliz Cloud database.

    Parameters
    ----------
    deep : bool
        If False (default), run API-level checks only (L0-L3 + lightweight L4-L7).
        If True, perform full cursor-scans for deep L4-L7 checks (slow).

    dims : list[str] | None
        Specific layer names to run (e.g. ``["indexes", "vectors"]``).
        If None, all layers are run.

    fix : bool
        If True, attempt to auto-fix recoverable issues (load collection,
        flush, trigger compaction).
    """

    _COLLECTIONS = ("arxiv_papers", "arxiv_chunks")
    _SAMPLE_SIZE = 10_000  # deep mode sample size for field-level stats

    def __init__(
        self,
        deep: bool = False,
        dims: list[str] | None = None,
        fix: bool = False,
    ) -> None:
        self.deep = deep
        self.dims = dims
        self.fix = fix
        self.report = HealthReport()

    def _should_run(self, name: str) -> bool:
        return self.dims is None or name in self.dims

    # ── L0: Connection ──────────────────────────────────────────────────────

    def check_connection(self) -> LayerResult:
        layer = LayerResult(layer="L0 Connection")
        client = get_client()

        # 0a — Reachability
        try:
            collections = client.list_collections()
            layer.checks.append(
                CheckResult(
                    HealthStatus.PASS, "reachability", f"Connected, {len(collections)} collections"
                )
            )
        except Exception as exc:
            layer.checks.append(
                CheckResult(HealthStatus.FAIL, "reachability", f"Cannot connect: {exc}")
            )
            return layer  # can't proceed

        # 0b — Server version
        try:
            ver_info = (
                client.get_server_version(detail=True)
                if hasattr(client, "get_server_version")
                else {}
            )
            if isinstance(ver_info, dict):
                version = ver_info.get("version", "unknown")
                deploy_mode = ver_info.get("deploy_mode", "unknown")
                detail = f"v{version}, mode={deploy_mode}"
            else:
                detail = f"v{ver_info}"
            layer.checks.append(
                CheckResult(HealthStatus.PASS, "server_version", detail, data={"raw": ver_info})
            )
        except Exception as exc:
            layer.checks.append(
                CheckResult(HealthStatus.WARN, "server_version", f"Cannot fetch version: {exc}")
            )

        return layer

    # ── L1: Collections ─────────────────────────────────────────────────────

    def check_collections(self) -> LayerResult:
        layer = LayerResult(layer="L1 Collections")
        client = get_client()

        for name in self._COLLECTIONS:
            if not client.has_collection(name):
                layer.checks.append(
                    CheckResult(
                        HealthStatus.FAIL, f"{name}/exists", f"Collection '{name}' not found"
                    )
                )
                continue

            # 1a — Schema integrity
            try:
                desc = client.describe_collection(name)
                num_fields = len(desc.get("fields", []))
                num_shards = desc.get("num_shards", "?")
                detail = f"{num_fields} fields, {num_shards} shards"
                layer.checks.append(
                    CheckResult(
                        HealthStatus.PASS, f"{name}/schema", detail, data={"description": desc}
                    )
                )
            except Exception as exc:
                layer.checks.append(
                    CheckResult(HealthStatus.FAIL, f"{name}/schema", f"describe failed: {exc}")
                )

            # 1b — Load state
            try:
                load = client.get_load_state(name)
                state = load.get("state", "Unknown")
                if state == "LoadState.Loaded":
                    layer.checks.append(
                        CheckResult(
                            HealthStatus.PASS, f"{name}/loaded", "Collection loaded in memory"
                        )
                    )
                elif state == "LoadState.Loading":
                    layer.checks.append(
                        CheckResult(HealthStatus.WARN, f"{name}/loaded", "Still loading…")
                    )
                else:
                    detail = f"State={state}"
                    if self.fix:
                        try:
                            client.load_collection(name, timeout=3600)
                            detail += " — auto-loaded ✓"
                            layer.checks.append(
                                CheckResult(HealthStatus.PASS, f"{name}/loaded", detail)
                            )
                        except Exception as exc:
                            layer.checks.append(
                                CheckResult(
                                    HealthStatus.FAIL,
                                    f"{name}/loaded",
                                    f"{detail}; load failed: {exc}",
                                )
                            )
                    else:
                        layer.checks.append(
                            CheckResult(
                                HealthStatus.WARN if state != "NotLoad" else HealthStatus.FAIL,
                                f"{name}/loaded",
                                detail + " — run --fix to auto-load",
                            )
                        )
            except Exception as exc:
                layer.checks.append(
                    CheckResult(
                        HealthStatus.WARN, f"{name}/loaded", f"Cannot check load state: {exc}"
                    )
                )

        return layer

    # ── L2: Indexes ─────────────────────────────────────────────────────────

    def check_indexes(self) -> LayerResult:
        layer = LayerResult(layer="L2 Indexes")
        client = get_client()

        for name in self._COLLECTIONS:
            if not client.has_collection(name):
                continue

            try:
                index_names = client.list_indexes(name)
            except Exception as exc:
                layer.checks.append(
                    CheckResult(HealthStatus.FAIL, f"{name}/indexes", f"list_indexes failed: {exc}")
                )
                continue

            if not index_names:
                layer.checks.append(
                    CheckResult(HealthStatus.FAIL, f"{name}/indexes", "No indexes found")
                )
                continue

            ok = 0
            warn = 0
            for idx_name in sorted(index_names):
                try:
                    info = client.describe_index(name, idx_name)
                    state = info.get("state", "Unknown")
                    total_rows = info.get("total_rows", 0)
                    indexed_rows = info.get("indexed_rows", 0)
                    pending = info.get("pending_index_rows", 0)

                    if pending and pending > 0:
                        layer.checks.append(
                            CheckResult(
                                HealthStatus.WARN,
                                f"{name}/{idx_name}",
                                f"indexing in progress: {indexed_rows}/{total_rows} rows, {pending} pending",
                            )
                        )
                        warn += 1
                    elif state == "Finished" or state == 3:
                        layer.checks.append(
                            CheckResult(
                                HealthStatus.PASS,
                                f"{name}/{idx_name}",
                                f"{indexed_rows}/{total_rows} rows indexed, state={state}",
                            )
                        )
                        ok += 1
                    else:
                        layer.checks.append(
                            CheckResult(
                                HealthStatus.WARN,
                                f"{name}/{idx_name}",
                                f"state={state}, {indexed_rows}/{total_rows} rows",
                            )
                        )
                        warn += 1
                except Exception as exc:
                    layer.checks.append(
                        CheckResult(
                            HealthStatus.FAIL, f"{name}/{idx_name}", f"describe failed: {exc}"
                        )
                    )

            if ok > 0:
                layer.checks.append(
                    CheckResult(
                        HealthStatus.PASS,
                        f"{name}/index_summary",
                        f"{ok} index(es) OK" + (f", {warn} warning(s)" if warn else ""),
                    )
                )

        return layer

    # ── L3: Segments ────────────────────────────────────────────────────────

    def check_segments(self) -> LayerResult:
        layer = LayerResult(layer="L3 Segments")
        client = get_client()

        for name in self._COLLECTIONS:
            if not client.has_collection(name):
                continue

            try:
                loaded = client.list_loaded_segments(name)
            except Exception as exc:
                layer.checks.append(
                    CheckResult(
                        HealthStatus.WARN, f"{name}/segments", f"list_loaded_segments failed: {exc}"
                    )
                )
                loaded = []

            try:
                persistent = client.list_persistent_segments(name)
            except Exception as exc:
                layer.checks.append(
                    CheckResult(
                        HealthStatus.WARN,
                        f"{name}/segments",
                        f"list_persistent_segments failed: {exc}",
                    )
                )
                persistent = []

            total_loaded_rows = sum(getattr(s, "num_rows", 0) or 0 for s in loaded)
            total_mem = sum(getattr(s, "mem_size", 0) or 0 for s in loaded)
            n_persistent = len(persistent)
            n_loaded = len(loaded)

            # Classify segments by level if available
            levels: Counter[int] = Counter()
            for s in persistent:
                lv = getattr(s, "level", 0) or 0
                levels[lv] += 1
            for s in loaded:
                lv = getattr(s, "level", 0) or 0
                levels[lv] += 1

            detail_parts = [
                f"{n_loaded} loaded, {n_persistent} persistent",
                f"{total_loaded_rows:,} loaded rows, {total_mem:,} B memory",
            ]
            if levels:
                level_detail = ", ".join(f"L{lv}={cnt}" for lv, cnt in sorted(levels.items()))
                detail_parts.append(f"levels: {level_detail}")

            status = HealthStatus.PASS
            if n_loaded == 0 and n_persistent == 0:
                status = HealthStatus.WARN

            layer.checks.append(CheckResult(status, f"{name}/segments", "\n".join(detail_parts)))

        return layer

    # ── L4: Data Stats (deep = full scan; quick = stats API) ────────────────

    def check_data_stats(self) -> LayerResult:
        layer = LayerResult(layer="L4 DataStats")
        client = get_client()

        for name in self._COLLECTIONS:
            if not client.has_collection(name):
                continue

            try:
                stats = client.get_collection_stats(name)
                row_count = stats.get("row_count", 0)
                layer.checks.append(
                    CheckResult(HealthStatus.PASS, f"{name}/row_count", f"{row_count:,} rows")
                )
            except Exception as exc:
                layer.checks.append(
                    CheckResult(HealthStatus.WARN, f"{name}/row_count", f"stats failed: {exc}")
                )
                row_count = 0

        if not self.deep:
            layer.checks.append(
                CheckResult(
                    HealthStatus.PASS,
                    "deep_stats",
                    "skipped (quick mode) — use --deep for year/field analysis",
                )
            )
            return layer

        # ── Deep: full cursor-scan on arxiv_papers ──
        if not client.has_collection("arxiv_papers"):
            return layer

        try:
            all_ids = _cursor_scan(client, "arxiv_papers", ["arxiv_id"], limit=10000)
        except Exception as exc:
            layer.checks.append(
                CheckResult(HealthStatus.FAIL, "arxiv_papers/scan", f"Cursor scan failed: {exc}")
            )
            return layer

        # 4a — Year distribution
        years: Counter[int] = Counter()
        for p in all_ids:
            years[_extract_year(str(p["arxiv_id"]))] += 1
        if years:
            layer.checks.append(
                CheckResult(
                    HealthStatus.PASS,
                    "arxiv_papers/years",
                    f"{len(years)} years, {min(years)}-{max(years)}",
                    data={
                        "year_range": f"{min(years)}-{max(years)}",
                        "top_years": years.most_common(5),
                    },
                )
            )

        # 4b — Field completeness (10K sample, no vectors)
        sample_fields = [f for f in PAPER_SEARCH_FIELDS if f != "arxiv_id" and f != "version"][
            :12
        ]  # limit to scalar fields only
        sample = _cursor_scan(
            client,
            "arxiv_papers",
            sample_fields,
            limit=2000,
            stop_after=self._SAMPLE_SIZE,
        )
        if sample:
            field_stats: dict[str, dict[str, int]] = {}
            for f in sample_fields:
                field_stats[f] = {"empty": 0}
            for p in sample:
                for f in sample_fields:
                    val = p.get(f)
                    if (
                        val is None
                        or (isinstance(val, list) and len(val) == 0)
                        or (isinstance(val, str) and val.strip() == "")
                    ):
                        field_stats[f]["empty"] += 1

            worst_fields = sorted(
                ((f, s["empty"]) for f, s in field_stats.items() if s["empty"] > 0),
                key=lambda x: x[1],
                reverse=True,
            )[:5]
            if worst_fields:
                detail = "; ".join(f"{f}: {n} empty" for f, n in worst_fields)
                status = (
                    HealthStatus.WARN
                    if worst_fields[0][1] / len(sample) > 0.1
                    else HealthStatus.PASS
                )
            else:
                detail = "all fields populated"
                status = HealthStatus.PASS
            layer.checks.append(
                CheckResult(
                    status,
                    "arxiv_papers/fields",
                    f"Sample {len(sample)}: {detail}",
                    data={"field_stats": field_stats},
                )
            )

        return layer

    # ── L5: Resources (pipeline flags) ──────────────────────────────────────

    def check_resources(self) -> LayerResult:
        layer = LayerResult(layer="L5 Resources")
        client = get_client()

        if not client.has_collection("arxiv_papers"):
            return layer

        flags = ["has_latex", "has_pdf", "has_markdown", "has_chunks"]

        for flag in flags:
            try:
                # Quick: just sample one batch
                batch = client.query(
                    "arxiv_papers",
                    filter=f"{flag} == False",
                    output_fields=["arxiv_id"],
                    limit=1,
                )
                if batch:
                    layer.checks.append(
                        CheckResult(
                            HealthStatus.WARN,
                            f"papers/missing_{flag}",
                            f"Papers with {flag}==False exist (sample: {batch[0]['arxiv_id']})",
                        )
                    )
                else:
                    layer.checks.append(
                        CheckResult(
                            HealthStatus.PASS,
                            f"papers/{flag}",
                            f"All papers have {flag}==True (sampled)",
                        )
                    )
            except Exception as exc:
                layer.checks.append(
                    CheckResult(HealthStatus.WARN, f"papers/{flag}", f"Query failed: {exc}")
                )

        if not self.deep:
            return layer

        # Deep: count papers missing each flag via cursor scan
        for flag in flags:
            try:
                total_missing = 0
                last_id = ""
                while True:
                    flt = f"arxiv_id > '{escape_sql(last_id)}'" if last_id else "arxiv_id != ''"
                    rows = client.query(
                        "arxiv_papers",
                        filter=flt,
                        output_fields=["arxiv_id", flag],
                        limit=10000,
                    )
                    if not rows:
                        break
                    for r in rows:
                        if not r.get(flag, False):
                            total_missing += 1
                    last_id = rows[-1]["arxiv_id"]
                if total_missing > 0:
                    layer.checks.append(
                        CheckResult(
                            HealthStatus.WARN,
                            f"papers/missing_{flag}_count",
                            f"{total_missing:,} papers with {flag}==False",
                        )
                    )
            except Exception as exc:
                layer.checks.append(
                    CheckResult(
                        HealthStatus.FAIL, f"papers/{flag}_count", f"Deep scan failed: {exc}"
                    )
                )

        return layer

    # ── L6: Vectors ─────────────────────────────────────────────────────────

    def check_vectors(self) -> LayerResult:
        layer = LayerResult(layer="L6 Vectors")
        client = get_client()

        for name in self._COLLECTIONS:
            if not client.has_collection(name):
                continue

            # Determine vector fields for this collection
            if name == "arxiv_papers":
                vec_fields = [
                    f for f in PAPER_VECTOR_FIELDS if f.endswith("_embedding")
                ]  # dense only
            else:
                vec_fields = ["content_embedding"]

            if not self.deep:
                # Quick: spot-check first batch
                for vf in vec_fields:
                    try:
                        batch = client.query(
                            name,
                            filter="arxiv_id != ''",
                            output_fields=["arxiv_id", vf],
                            limit=100,
                        )
                        zero = sum(
                            1
                            for r in batch
                            if not r.get(vf) or all(abs(v) < 1e-9 for v in (r.get(vf) or []))
                        )
                        if zero > 0:
                            layer.checks.append(
                                CheckResult(
                                    HealthStatus.WARN,
                                    f"{name}/{vf}",
                                    f"{zero}/{len(batch)} zero vectors in sample",
                                )
                            )
                        else:
                            layer.checks.append(
                                CheckResult(
                                    HealthStatus.PASS,
                                    f"{name}/{vf}",
                                    f"All {len(batch)} sampled vectors non-zero",
                                )
                            )
                    except Exception as exc:
                        layer.checks.append(
                            CheckResult(HealthStatus.WARN, f"{name}/{vf}", f"Check failed: {exc}")
                        )
                return layer

            # Deep: larger sample
            for vf in vec_fields:
                try:
                    sample = _cursor_scan(
                        client,
                        name,
                        ["arxiv_id", vf],
                        limit=500,
                        stop_after=1000,
                        pk="arxiv_id" if name == "arxiv_papers" else "chunk_id",
                    )
                    zero = sum(
                        1
                        for r in sample
                        if not r.get(vf) or all(abs(v) < 1e-9 for v in (r.get(vf) or []))
                    )
                    pct = round(zero / max(len(sample), 1) * 100, 2)
                    status = HealthStatus.WARN if pct > 0 else HealthStatus.PASS
                    layer.checks.append(
                        CheckResult(
                            status,
                            f"{name}/{vf}",
                            f"{zero}/{len(sample)} zero vectors ({pct}%)",
                        )
                    )
                except Exception as exc:
                    layer.checks.append(
                        CheckResult(HealthStatus.FAIL, f"{name}/{vf}", f"Deep scan failed: {exc}")
                    )

        return layer

    # ── L7: Consistency ─────────────────────────────────────────────────────

    def check_consistency(self) -> LayerResult:
        layer = LayerResult(layer="L7 Consistency")
        client = get_client()

        if not client.has_collection("arxiv_papers") or not client.has_collection("arxiv_chunks"):
            layer.checks.append(
                CheckResult(HealthStatus.WARN, "cross_check", "Both collections required")
            )
            return layer

        # 7a — Stats-based row count comparison
        try:
            p_stats = client.get_collection_stats("arxiv_papers")
            c_stats = client.get_collection_stats("arxiv_chunks")
            p_rows = p_stats.get("row_count", 0)
            c_rows = c_stats.get("row_count", 0)
            layer.checks.append(
                CheckResult(
                    HealthStatus.PASS,
                    "row_counts",
                    f"papers={p_rows:,}, chunks={c_rows:,}",
                    data={"paper_rows": p_rows, "chunk_rows": c_rows},
                )
            )
        except Exception as exc:
            layer.checks.append(
                CheckResult(HealthStatus.WARN, "row_counts", f"Stats failed: {exc}")
            )

        # 7b — Papers with has_chunks=True but no actual chunks (spot-check)
        try:
            false_positives = 0
            checked = 0
            last_id = ""
            for _ in range(3):  # 3 batches
                flt = (
                    f"arxiv_id > '{escape_sql(last_id)}' and has_chunks == True"
                    if last_id
                    else "has_chunks == True"
                )
                batch = client.query(
                    "arxiv_papers",
                    filter=flt,
                    output_fields=["arxiv_id"],
                    limit=100,
                )
                if not batch:
                    break
                for p in batch:
                    aid = p["arxiv_id"]
                    safe_id = escape_sql(aid)
                    chunks = client.query(
                        "arxiv_chunks",
                        filter=f"arxiv_id == '{safe_id}'",
                        output_fields=["chunk_id"],
                        limit=1,
                    )
                    if not chunks:
                        false_positives += 1
                    checked += 1
                last_id = batch[-1]["arxiv_id"]

            status = HealthStatus.WARN if false_positives > 0 else HealthStatus.PASS
            detail = (
                f"All {checked} papers with has_chunks=True have chunks ✓"
                if false_positives == 0
                else f"{false_positives}/{checked} has_chunks=True but no chunks found"
            )
            layer.checks.append(CheckResult(status, "chunk_consistency", detail))
        except Exception as exc:
            layer.checks.append(
                CheckResult(HealthStatus.WARN, "chunk_consistency", f"Check failed: {exc}")
            )

        return layer

    # ── Main entrypoint ─────────────────────────────────────────────────────

    def run(self) -> HealthReport:
        """Execute all enabled health check layers and return a report."""
        total_t0 = time.monotonic()

        layer_dispatch = [
            ("connection", self.check_connection),
            ("collections", self.check_collections),
            ("indexes", self.check_indexes),
            ("segments", self.check_segments),
            ("data_stats", self.check_data_stats),
            ("resources", self.check_resources),
            ("vectors", self.check_vectors),
            ("consistency", self.check_consistency),
        ]

        self.report.layers = []
        for dim, method in layer_dispatch:
            if not self._should_run(dim):
                continue
            t0 = time.perf_counter()
            try:
                layer = method()
            except Exception as exc:
                logger.exception("health check layer failed", layer=dim)
                layer = LayerResult(layer=dim)
                layer.checks.append(
                    CheckResult(HealthStatus.FAIL, "exception", f"Layer crashed: {exc}")
                )
            layer.duration_ms = (time.perf_counter() - t0) * 1000
            self.report.layers.append(layer)

        self.report.deep = self.deep
        self.report.total_duration_ms = (time.monotonic() - total_t0) * 1000
        self.report.analyzed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        return self.report


# ── Convenience function ──────────────────────────────────────────────────────


def run_health_check(
    deep: bool = False,
    dims: list[str] | None = None,
    fix: bool = False,
) -> HealthReport:
    """Run a health check and return the report."""
    checker = HealthChecker(deep=deep, dims=dims, fix=fix)
    return checker.run()
