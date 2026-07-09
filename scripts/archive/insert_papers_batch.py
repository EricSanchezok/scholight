import os
"""Batch insert arxiv_papers from Parquet into Zilliz Cloud."""
import sys, time, gc
from pathlib import Path
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TOKEN = os.environ.get("COMPASS_ZILLIZ_TOKEN", "")
URI = "https://in05-d432d46d6c77308.serverless.ali-cn-hangzhou.cloud.zilliz.com.cn"

from pymilvus import MilvusClient
c = MilvusClient(uri=URI, token=TOKEN)

PAPERS = Path("/inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/academic-data/zilliz-import/arxiv_papers")
files = sorted([f for f in PAPERS.rglob("*.parquet") if not f.name.endswith(".bak")])

BATCH = 1000
t0 = time.monotonic()
total = ok = 0

for fi, pf in enumerate(files):
    df = pd.read_parquet(pf)
    n = len(df)
    for start in range(0, n, BATCH):
        batch = []
        end = min(start + BATCH, n)
        for i in range(start, end):
            row = {}
            for col in df.columns:
                val = df.iloc[i][col]
                if col == "abstract_embedding" and hasattr(val, "tolist"):
                    row[col] = val.tolist()
                elif hasattr(val, "item") and not isinstance(val, str):
                    if "bool" in str(df[col].dtype):
                        row[col] = bool(val)
                    else:
                        row[col] = val.item()
                elif isinstance(val, np.ndarray):
                    row[col] = val.tolist()
                else:
                    row[col] = val
            batch.append(row)
        try:
            result = c.insert("arxiv_papers", batch)
            ok += result["insert_count"]
            total += result["insert_count"]
        except Exception as e:
            print(f"FAIL [{fi+1}/{len(files)}] batch start={start}: {e}", flush=True)
    total += 0  # already counted
    if (fi + 1) % 10 == 0:
        elapsed = time.monotonic() - t0
        print(f"[{fi+1}/{len(files)}] inserted={ok:,} rows ({elapsed:.0f}s, {ok/elapsed:.0f} rows/s)", flush=True)

elapsed = time.monotonic() - t0
print(f"\nDONE: {ok:,} rows in {elapsed:.0f}s ({elapsed/60:.1f}min, {ok/elapsed:.0f} rows/s)")
