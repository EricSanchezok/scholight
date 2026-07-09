import os

"""Upload all arxiv_chunks Parquet files to Zilliz Managed Volume volume-sii.
Resume-safe + per-file timeout via ThreadPoolExecutor (handles C extensions).
Shared VolumeFileManager for speed; fresh one only on hang.
"""

import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pymilvus.bulk_writer.volume_file_manager import VolumeFileManager

TOKEN = os.environ.get("SCHOLIGHT_ZILLIZ_TOKEN", "")
CHUNKS = Path(
    "/inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/academic-data/zilliz-import/arxiv_chunks"
)
LOG_FILE = Path(
    "/inspire/hdd/project/multi-agent/niexiaohang-25130061/scholight/logs/upload_chunks.log"
)
FILE_TIMEOUT = 120  # seconds per file


def _make_vfm() -> VolumeFileManager:
    return VolumeFileManager(
        cloud_endpoint="https://api.cloud.zilliz.com.cn",
        api_key=TOKEN,
        volume_name="volume-sii",
    )


# ── Build resume set ───────────────────────────────────────────────
done: set[str] = set()
if LOG_FILE.exists():
    for line in LOG_FILE.read_text().splitlines():
        if "Uploaded file" in line and ".parquet" in line:
            try:
                start = line.index("/inspire")
                end = line.index(".parquet", start) + 8
                done.add(line[start:end])
            except ValueError:
                continue
print(f"Resume: {len(done)} files already uploaded, skipping.", flush=True)

files = sorted([f for f in CHUNKS.rglob("*.parquet") if not f.name.endswith(".bak")])
vfm = _make_vfm()
t0 = time.monotonic()
ok = fail = 0

for i, f in enumerate(files):
    if str(f) in done:
        ok += 1
        continue
    rel = f.relative_to(CHUNKS)
    target = f"chunks/{rel.parent.name}/"

    executor = ThreadPoolExecutor(max_workers=1)
    fut = executor.submit(vfm.upload_file_to_volume, str(f), target)
    try:
        fut.result(timeout=FILE_TIMEOUT)
        ok += 1
    except FutureTimeout:
        fail += 1
        print(f"FAIL [{i + 1}/{len(files)}] {f.name}: timed out after {FILE_TIMEOUT}s", flush=True)
        # Hang likely means VFM's underlying connection is stuck — replace it
        vfm = _make_vfm()
    except Exception as e:
        fail += 1
        print(f"FAIL [{i + 1}/{len(files)}] {f.name}: {e}", flush=True)
    executor.shutdown(wait=False)

    if (i + 1) % 100 == 0:
        elapsed = time.monotonic() - t0
        print(f"[{i + 1}/{len(files)}] ok={ok} fail={fail} ({elapsed:.0f}s)", flush=True)

elapsed = time.monotonic() - t0
print(f"\nDONE: {ok} success, {fail} failed in {elapsed:.0f}s ({elapsed / 60:.1f}min)", flush=True)
