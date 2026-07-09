import sys, time
from collections import defaultdict
from pathlib import Path
import pandas as pd

PAPERS = Path("/inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/academic-data/zilliz-import/arxiv_papers")

def _build():
    best = {}
    for pf in sorted(PAPERS.rglob("*.parquet")):
        if pf.name.endswith(".bak"): continue
        df = pd.read_parquet(pf, columns=["arxiv_id","version"])
        for _, r in df.iterrows():
            a = r["arxiv_id"]; v = int(r["version"])
            if a not in best or v > best[a][0] or (v == best[a][0] and pf.name > best[a][1]):
                best[a] = (v, str(pf))
    tmp = defaultdict(set)
    for a, (_, f) in best.items(): tmp[f].add(a)
    print(f"pass-1: distinct={len(best):,} keepers={len(tmp)}", flush=True)
    return dict(tmp)

def dedup():
    t0 = time.monotonic()
    km = _build()
    kt = dt = ft = 0
    for pf in sorted(PAPERS.rglob("*.parquet")):
        if pf.name.endswith(".bak"): continue
        ft += 1; fn = str(pf)
        if fn not in km: pf.rename(pf.with_suffix(".parquet.bak")); continue
        df = pd.read_parquet(pf)
        kept = df[df["arxiv_id"].isin(km[fn])]
        n_cross = len(df) - len(kept)
        n_intra = 0
        if kept["arxiv_id"].duplicated().any():
            nb = len(kept)
            kept = kept.sort_values("version", ascending=False).drop_duplicates(subset="arxiv_id", keep="first")
            n_intra = nb - len(kept)
        kt += len(kept); dt += n_cross + n_intra
        pf.rename(pf.with_suffix(".parquet.bak"))
        kept.to_parquet(pf, index=False)
        if ft % 20 == 0: print(f"  [{ft:>3}] kept={kt:,} dropped={dt:,} ({int(time.monotonic()-t0)}s)", flush=True)
    e = time.monotonic()-t0
    print(f"\nDONE: {ft} files kept={kt:,} dropped={dt:,} {e/60:.1f}min", flush=True)

dedup()
