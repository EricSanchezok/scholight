"""Deduplicate arxiv_chunks Parquet by chunk_id (PK), keeping first occurrence.
Only acts on files that actually contain duplicates (9 of 6218). Originals renamed to *.bak."""

import sys, time
from pathlib import Path
import pandas as pd

CHUNKS = Path(
    "/inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/academic-data/zilliz-import/arxiv_chunks"
)


def dedup():
    t0 = time.monotonic()
    total_files = len(list(CHUNKS.rglob("*.parquet")))
    fixed = 0
    for pf in sorted(CHUNKS.rglob("*.parquet")):
        if pf.name.endswith(".bak"):
            continue
        df = pd.read_parquet(pf, columns=["chunk_id"])
        n = len(df)
        n_unique = df["chunk_id"].nunique()
        if n == n_unique:
            continue  # clean — skip rewrite
        # Has dupes — read full file, dedup, rewrite
        df_full = pd.read_parquet(pf)
        before = len(df_full)
        df_clean = df_full.drop_duplicates(subset="chunk_id", keep="first")
        after = len(df_clean)
        bak = pf.with_suffix(".parquet.bak")
        pf.rename(bak)
        df_clean.to_parquet(pf, index=False)
        fixed += 1
        print(f"  {pf.name}: {before:,} → {after:,} rows (removed {before - after})", flush=True)
    elapsed = time.monotonic() - t0
    print(
        f"\nDone: {fixed}/{total_files} files had duplicates, cleaned in {elapsed:.0f}s", flush=True
    )


dedup()
