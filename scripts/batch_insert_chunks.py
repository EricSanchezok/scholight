import os
"""Batch insert arxiv_chunks from Parquet into Zilliz Cloud."""
import sys, time
from pathlib import Path
import pandas as pd, numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TOKEN = os.environ.get("COMPASS_ZILLIZ_TOKEN", "")
URI = "https://in05-d432d46d6c77308.serverless.ali-cn-hangzhou.cloud.zilliz.com.cn"
from pymilvus import MilvusClient
c = MilvusClient(uri=URI, token=TOKEN)

CHUNKS = Path("/inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/academic-data/zilliz-import/arxiv_chunks")
files = sorted([f for f in CHUNKS.rglob("*.parquet") if not f.name.endswith(".bak")])
print(f"Files: {len(files)}, starting insert...", flush=True)

BATCH = 1000
t0 = time.monotonic()
ok = 0

for fi, pf in enumerate(files):
    df = pd.read_parquet(pf)
    n = len(df)
    for s in range(0, n, BATCH):
        e = min(s + BATCH, n)
        batch = []
        for i in range(s, e):
            row = {}
            for col in df.columns:
                v = df.iloc[i][col]
                if isinstance(v, np.ndarray):
                    row[col] = v.tolist()
                elif hasattr(v, "item"):
                    row[col] = v.item()
                else:
                    row[col] = v
            batch.append(row)
        try:
            res = c.insert("arxiv_chunks", batch)
            ok += res["insert_count"]
        except Exception as ex:
            print(f"FAIL [{fi+1}]@{s}: {ex}", flush=True)
    if (fi + 1) % 200 == 0:
        elapsed = time.monotonic() - t0
        rate = ok / max(elapsed, 0.1)
        print(f"[{fi+1}/{len(files)}] ok={ok:,} rows ({elapsed:.0f}s, {rate:.0f} r/s)", flush=True)

elapsed = time.monotonic() - t0
rate = ok / max(elapsed, 0.1)
print(f"\nDONE: {ok:,} rows in {elapsed:.0f}s ({elapsed/60:.1f}min, {rate:.0f} r/s)", flush=True)
