import os

"""Upload all deduped arxiv_papers Parquet files to Zilliz Managed Volume volume-sii."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pymilvus.bulk_writer.volume_file_manager import VolumeFileManager

TOKEN = os.environ.get("SCHOLIGHT_ZILLIZ_TOKEN", "")
PAPERS = Path(
    "/inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/academic-data/zilliz-import/arxiv_papers"
)
files = sorted([f for f in PAPERS.rglob("*.parquet") if not f.name.endswith(".bak")])

vfm = VolumeFileManager(
    cloud_endpoint="https://api.cloud.zilliz.com.cn", api_key=TOKEN, volume_name="volume-sii"
)
t0 = time.monotonic()
ok = fail = 0
for i, f in enumerate(files):
    rel = f.relative_to(PAPERS)
    target = f"papers/{rel.parent.name}/"
    try:
        vfm.upload_file_to_volume(source_file_path=str(f), target_volume_path=target)
        ok += 1
    except Exception as e:
        fail += 1
        print(f"FAIL [{i + 1}/{len(files)}] {f.name}: {e}", flush=True)
    if (i + 1) % 20 == 0:
        elapsed = time.monotonic() - t0
        print(f"[{i + 1}/{len(files)}] ok={ok} fail={fail} ({elapsed:.0f}s)", flush=True)
elapsed = time.monotonic() - t0
print(f"\nDONE: {ok} success, {fail} failed in {elapsed:.0f}s ({elapsed / 60:.1f}min)", flush=True)
