#!/usr/bin/env python3
"""data_health_report.py — Milvus arXiv 数据库全面健康检查.

Dimensions: total count, year distribution, field completeness,
category distribution, version stats, embedding quality, era coverage.
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

_project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_project_root))

from compass.store.client import get_client  # noqa: E402


def esc(s: str) -> str:
    return s.replace("'", "\\'")


def query_all(
    client: Any, collection: str, output_fields: list[str], limit: int = 10000, stop_after: int = 0
) -> list[dict[str, Any]]:
    results = []
    last_id = ""
    while True:
        flt = f"arxiv_id > '{esc(last_id)}'" if last_id else "arxiv_id != ''"
        batch = client.query(collection, filter=flt, output_fields=output_fields, limit=limit)
        if not batch:
            break
        results.extend(batch)
        last_id = batch[-1]["arxiv_id"]
        if stop_after and len(results) >= stop_after:
            return results[:stop_after]
    return results


def extract_year(aid: str) -> int:
    if "/" in aid:
        yy = aid.split("/")[1][:2]
        return 1900 + int(yy) if int(yy) > 90 else 2000 + int(yy)
    return 2000 + int(aid[:2])


def main() -> None:
    client = get_client()
    t0 = time.monotonic()
    report: dict[str, Any] = {}

    # ---- 1. Total count (full scan) ----
    print("[1/7] Total count (full scan)...", flush=True)
    all_ids = query_all(client, "arxiv_papers", ["arxiv_id"])
    total = len(all_ids)
    report["total_papers"] = total

    # ---- 2. Year distribution ----
    print("[2/7] Year distribution...", flush=True)
    years = Counter()
    for p in all_ids:
        years[extract_year(str(p["arxiv_id"]))] += 1
    report["year_distribution"] = dict(sorted(years.items()))
    report["year_count"] = len(years)
    report["year_min"] = min(years.keys())
    report["year_max"] = max(years.keys())

    # ---- 3. Field completeness (10K sample, NO vector) ----
    print("[3/7] Field completeness (10K sample)...", flush=True)
    fields = [
        "arxiv_id",
        "title",
        "abstract",
        "authors",
        "categories",
        "created",
        "updated",
        "version",
        "updated_history",
        "license",
        "comments",
        "doi",
        "journal_ref",
        "acm_class",
    ]
    sample = query_all(client, "arxiv_papers", fields, limit=2000, stop_after=10000)

    field_stats = {}
    for f in fields:
        field_stats[f] = {"empty": 0, "truncated": 0}

    for p in sample:
        for f in fields:
            val = p.get(f)
            if f in ("arxiv_id", "version"):
                continue  # PK / always present
            if val is None:
                field_stats[f]["empty"] += 1
            elif isinstance(val, (list,)):
                if len(val) == 0:
                    field_stats[f]["empty"] += 1
            elif isinstance(val, str) and val.strip() == "":
                field_stats[f]["empty"] += 1

    # Truncation check: byte level, only where max_length is known
    for p in sample:
        # title: 2048 bytes
        t = str(p.get("title", "") or "")
        if len(t.encode("utf-8")) >= 2046:
            field_stats["title"]["truncated"] += 1
        # abstract: 16384 bytes
        a = str(p.get("abstract", "") or "")
        if len(a.encode("utf-8")) >= 16380:
            field_stats["abstract"]["truncated"] += 1
        # authors: 256 bytes each
        for au in p.get("authors") or []:
            if len(au.encode("utf-8")) >= 254:
                field_stats["authors"]["truncated"] += 1
                break

    report["field_completeness"] = {
        f: {"empty_pct": round(s["empty"] / len(sample) * 100, 2), "truncated": s["truncated"]}
        for f, s in field_stats.items()
    }

    # ---- 4. Category distribution ----
    print("[4/7] Category distribution...", flush=True)
    cats = Counter()
    for p in sample:
        for c in p.get("categories") or []:
            cats[c] += 1
    report["top_categories"] = cats.most_common(20)

    # ---- 5. Version stats ----
    print("[5/7] Version distribution...", flush=True)
    versions = Counter()
    has_history = 0
    for p in sample:
        versions[max(p.get("version", 1) or 1, 1) % 10] += 1  # clamp at 10+
        if p.get("updated_history") and len(p["updated_history"]) > 0:
            has_history += 1
    report["version_distribution"] = dict(sorted(versions.items()))
    report["updated_history_coverage_pct"] = round(has_history / len(sample) * 100, 2)

    # ---- 6. Embedding quality (separate query) ----
    print("[6/7] Embedding quality (1K sample)...", flush=True)
    emb_sample = query_all(
        client, "arxiv_papers", ["arxiv_id", "abstract_embedding"], limit=500, stop_after=1000
    )
    zero_vec = sum(
        1
        for p in emb_sample
        if not p.get("abstract_embedding") or all(abs(v) < 1e-9 for v in p["abstract_embedding"])
    )
    report["embedding_quality"] = {
        "sample_size": len(emb_sample),
        "zero_vector_pct": round(zero_vec / max(len(emb_sample), 1) * 100, 2),
        "has_embedding_pct": round((len(emb_sample) - zero_vec) / max(len(emb_sample), 1) * 100, 2),
    }

    # ---- 7. Era coverage + state ----
    print("[7/7] Era coverage & pipeline status...", flush=True)
    pre2007 = sum(v for y, v in years.items() if y < 2007)
    post2007 = sum(v for y, v in years.items() if y >= 2007)
    report["era_coverage"] = {
        "pre_2007_papers": pre2007,
        "post_2007_papers": post2007,
        "pre_2007_pct": round(pre2007 / total * 100, 2),
        "post_2007_pct": round(post2007 / total * 100, 2),
    }
    # Year gaps
    all_years = list(range(report["year_min"], report["year_max"] + 1))
    gaps = [y for y in all_years if years.get(y, 0) == 0]
    report["year_gaps"] = gaps if gaps else "NONE — continuous coverage"

    # Pipeline status — count papers missing any resource flag
    paper_sample = client.query(
        "arxiv_papers",
        filter="arxiv_id != ''",
        output_fields=[
            "arxiv_id",
            "has_latex",
            "has_pdf",
            "has_markdown",
            "has_content_list",
            "has_chunks",
        ],
        limit=10000,
    )
    missing = 0
    for r in paper_sample:
        if not all(
            (
                r.get("has_latex"),
                r.get("has_pdf"),
                r.get("has_markdown"),
                r.get("has_content_list"),
                r.get("has_chunks"),
            )
        ):
            missing += 1
    report["papers_incomplete_count"] = missing

    elapsed = time.monotonic() - t0
    report["analysis_duration_seconds"] = round(elapsed, 1)
    report["analyzed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # ---- Output ----
    output = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(f"\n{'=' * 60}\n{output}")

    out_path = _project_root / "logs" / "data_health_report.json"
    with open(out_path, "w") as f:
        f.write(output)
    print(f"\nReport saved to: {out_path}")
    client.close()


if __name__ == "__main__":
    main()
