#!/usr/bin/env python3
"""Auto-generated Marker batch runner — 4 GPUs, 8 workers."""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

PDF_PATHS = [
    "/inspire/hdd/project/multi-agent/niexiaohang-25130061/scholight/data/2508.20033v2.pdf",
    "/inspire/hdd/project/multi-agent/niexiaohang-25130061/scholight/data/2510.03120v2.pdf",
    "/inspire/hdd/project/multi-agent/niexiaohang-25130061/scholight/data/2512.22716.pdf",
    "/inspire/hdd/project/multi-agent/niexiaohang-25130061/scholight/data/2601.03192.pdf",
    "/inspire/hdd/project/multi-agent/niexiaohang-25130061/scholight/data/2601.15307v1.pdf",
    "/inspire/hdd/project/multi-agent/niexiaohang-25130061/scholight/data/2602.20493.pdf",
    "/inspire/hdd/project/multi-agent/niexiaohang-25130061/scholight/data/2603.28428.pdf",
    "/inspire/hdd/project/multi-agent/niexiaohang-25130061/scholight/data/2605.08374v3.pdf",
]
OUTPUT_DIR = Path(
    "/inspire/hdd/project/multi-agent/niexiaohang-25130061/scholight/data/marker_output"
)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
NUM_GPUS = 4
MAX_WORKERS = 8


def process_one(args: tuple) -> dict:
    """Process a single PDF. Pin to GPU, output .md + _content_list.json."""
    pdf_str, gpu_id = args
    os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_id)

    try:
        import torch
        from marker.converters.pdf import PdfConverter
        from marker.models import create_model_dict
        from marker.output import text_from_rendered

        from scholight.utils.marker import marker_block_to_content

        assert torch.cuda.is_available(), "CUDA not available!"
        gpu_name = torch.cuda.get_device_name(0)
        stem = Path(pdf_str).stem
        print(f"  [GPU {gpu_id}] starting {stem}.pdf ({gpu_name}) ...")
        sys.stdout.flush()

        t0 = time.monotonic()
        converter = PdfConverter(artifact_dict=create_model_dict())

        # Get Document (for content_list) + renderer (for markdown)
        document = converter.build_document(pdf_str)
        renderer = converter.resolve_dependencies(converter.renderer)
        rendered = renderer(document)
        text, _, images = text_from_rendered(rendered)

        # Extract content_list from all pages
        content_list = []
        for page_idx, page in enumerate(document.pages):
            for child in page.current_children:
                item = marker_block_to_content(child, document, page_idx)
                if item:
                    content_list.append(item)

        elapsed = time.monotonic() - t0

        # Write .md
        (OUTPUT_DIR / f"{stem}.md").write_text(text, encoding="utf-8")
        # Write _content_list.json
        (OUTPUT_DIR / f"{stem}_content_list.json").write_text(
            json.dumps(content_list, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(
            f"  ✅ [GPU {gpu_id}] {stem}.pdf: {elapsed:.1f}s, {len(text)} chars, "
            f"{len(content_list)} blocks, {len(images)} imgs"
        )
        sys.stdout.flush()
        return {
            "pdf": pdf_str,
            "time_s": elapsed,
            "chars": len(text),
            "blocks": len(content_list),
            "gpu": gpu_id,
        }
    except Exception:
        import traceback

        print(f"  ❌ [GPU {gpu_id}] {Path(pdf_str).name}: FAILED")
        traceback.print_exc()
        sys.stdout.flush()
        return {"pdf": pdf_str, "error": str(sys.exc_info()[1]), "gpu": gpu_id}


if __name__ == "__main__":
    # Round-robin: 8 PDFs across 4 GPUs → GPU 0 gets PDF 0,4; GPU 1 gets 1,5; etc.
    tasks = [(p, i % NUM_GPUS) for i, p in enumerate(PDF_PATHS)]
    print(f"🚀 Marker batch: {len(tasks)} PDFs on {NUM_GPUS} GPUs, {MAX_WORKERS} workers\n")
    for gid in range(NUM_GPUS):
        assigned = [Path(p).stem for p, g in tasks if g == gid]
        print(f"   GPU {gid}: {assigned}")

    mp_ctx = multiprocessing.get_context("spawn")
    t0 = time.monotonic()

    with ProcessPoolExecutor(max_workers=MAX_WORKERS, mp_context=mp_ctx) as pool:
        futures = {pool.submit(process_one, t): t for t in tasks}
        results = []
        for f in as_completed(futures):
            results.append(f.result())

    total_cpu = sum(r.get("time_s", 0) for r in results)
    total_chars = sum(r.get("chars", 0) for r in results)
    total_blocks = sum(r.get("blocks", 0) for r in results)
    failed = sum(1 for r in results if "error" in r)
    wall = time.monotonic() - t0
    print(
        f"\n✅ {len(results) - failed}/{len(results)} OK, "
        f"wall {wall:.1f}s, {total_chars:,} chars, {total_blocks} blocks"
    )
