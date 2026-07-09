#!/usr/bin/env python3
"""
Marker PDF 解析对比测试脚本

在启智 GPU notebook (4x4090 48GB) 上运行，Markdown → JSON 输出，
与已有 MinerU 结果自动对比。

用法:
    # 完整安装（首次运行）
    python scripts/test_marker.py --install

    # 只跑一篇看看效果
    python scripts/test_marker.py --quick

    # 全部 8 篇，全页
    python scripts/test_marker.py --all

    # 全部 8 篇，但每篇只跑前 4 页（快速对比）
    python scripts/test_marker.py --all --max-pages 4
"""

from __future__ import annotations

import argparse
import json
import os  # noqa: F401
import subprocess
import sys
import time
from pathlib import Path

# ============================================================
# 配置 — 全部用共享存储路径，从哪台 notebook 跑都一样
# ============================================================
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = DATA_DIR / "marker_output"

# ============================================================
# Install
# ============================================================
INSTALL_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail

echo "=== Installing Marker with GPU support ==="

# Check CUDA
python -c "import torch; assert torch.cuda.is_available(), 'CUDA not available!'; print(f'CUDA OK: {torch.cuda.device_count()} x {torch.cuda.get_device_name(0)}')"

# Install marker-pdf (includes surya-ocr + texify)
pip install marker-pdf -q

# Verify
python -c "from marker.converters.pdf import PdfConverter; from marker.models import create_model_dict; print('Marker OK')"
python -c "from marker.settings import settings; print(f'Default device: {settings.TORCH_DEVICE}')"

echo ""
echo "=== Marker installed! GPU: $(python -c 'import torch; print(torch.cuda.device_count())') ==="
"""


def cmd_install():
    """Run the install block."""
    script_path = PROJECT_ROOT / "scripts" / "_install_marker.sh"
    script_path.write_text(INSTALL_SCRIPT)
    script_path.chmod(0o755)
    subprocess.run(["bash", str(script_path)], check=True)


# ============================================================
# Marker 批量解析（并发）
# ============================================================
def run_marker_batch(
    pdf_paths: list[Path],
    workers: int = 4,
    max_pages: int | None = None,
) -> dict:
    """Run Marker on a list of PDFs using multi-process parallelization.

    Each worker process loads its own Marker model and handles one PDF.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"🚀 Marker 批量解析: {len(pdf_paths)} PDFs, {workers} workers")
    print(f"   Output: {OUTPUT_DIR}")

    # Use a separate launcher script to avoid pickling issues with Marker objects
    launcher = _write_batch_launcher(pdf_paths, OUTPUT_DIR, workers)
    t_start = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(launcher)],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    elapsed = time.monotonic() - t_start

    # Print stdout
    if result.stdout:
        print(result.stdout)

    if result.returncode != 0:
        print("❌ Marker batch failed:")
        print(result.stderr[-3000:])
        return {"status": "failed", "time_s": elapsed, "error": result.stderr[-500:]}

    print(f"✅ 全量完成 in {elapsed:.1f}s ({elapsed / 60:.1f} min)")
    return {"status": "ok", "time_s": elapsed}


def _write_batch_launcher(pdf_paths: list[Path], output_dir: Path, workers: int) -> Path:
    """Write a self-contained batch launcher — 4 GPUs, 8 workers, content_list output."""
    launcher_path = PROJECT_ROOT / "scripts" / "_marker_batch_runner.py"
    pdfs_repr = repr([str(p) for p in pdf_paths])
    num_gpus = 4
    launcher_path.write_text(f'''#!/usr/bin/env python3
"""Auto-generated Marker batch runner — {num_gpus} GPUs, {workers} workers."""
from __future__ import annotations
import json, os, sys, time, traceback, multiprocessing
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

PDF_PATHS = {pdfs_repr}
OUTPUT_DIR = Path("{output_dir}")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
NUM_GPUS = {num_gpus}
MAX_WORKERS = {workers}


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
        print(f"  [GPU {{gpu_id}}] starting {{stem}}.pdf ({{gpu_name}}) ...")
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
        (OUTPUT_DIR / f"{{stem}}.md").write_text(text, encoding="utf-8")
        # Write _content_list.json
        (OUTPUT_DIR / f"{{stem}}_content_list.json").write_text(
            json.dumps(content_list, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"  ✅ [GPU {{gpu_id}}] {{stem}}.pdf: {{elapsed:.1f}}s, {{len(text)}} chars, "
              f"{{len(content_list)}} blocks, {{len(images)}} imgs")
        sys.stdout.flush()
        return {{"pdf": pdf_str, "time_s": elapsed, "chars": len(text),
                "blocks": len(content_list), "gpu": gpu_id}}
    except Exception:
        import traceback
        print(f"  ❌ [GPU {{gpu_id}}] {{Path(pdf_str).name}}: FAILED")
        traceback.print_exc()
        sys.stdout.flush()
        return {{"pdf": pdf_str, "error": str(sys.exc_info()[1]), "gpu": gpu_id}}


if __name__ == "__main__":
    # Round-robin: 8 PDFs across 4 GPUs → GPU 0 gets PDF 0,4; GPU 1 gets 1,5; etc.
    tasks = [(p, i % NUM_GPUS) for i, p in enumerate(PDF_PATHS)]
    print(f"🚀 Marker batch: {{len(tasks)}} PDFs on {{NUM_GPUS}} GPUs, {{MAX_WORKERS}} workers\\n")
    for gid in range(NUM_GPUS):
        assigned = [Path(p).stem for p, g in tasks if g == gid]
        print(f"   GPU {{gid}}: {{assigned}}")

    mp_ctx = multiprocessing.get_context("spawn")
    t0 = time.monotonic()

    with ProcessPoolExecutor(max_workers=MAX_WORKERS, mp_context=mp_ctx) as pool:
        futures = {{pool.submit(process_one, t): t for t in tasks}}
        results = []
        for f in as_completed(futures):
            results.append(f.result())

    total_cpu = sum(r.get("time_s", 0) for r in results)
    total_chars = sum(r.get("chars", 0) for r in results)
    total_blocks = sum(r.get("blocks", 0) for r in results)
    failed = sum(1 for r in results if "error" in r)
    wall = time.monotonic() - t0
    print(f"\\n✅ {{len(results)-failed}}/{{len(results)}} OK, "
          f"wall {{wall:.1f}}s, {{total_chars:,}} chars, {{total_blocks}} blocks")
''')
    return launcher_path


# ============================================================
# Marker API 单篇解析（更灵活，可拿到 JSON metadata）
# ============================================================
def run_marker_single(
    pdf_path: Path,
    output_dir: Path | None = None,
    max_pages: int | None = None,
) -> tuple[Path, dict]:
    """Run Marker via Python API on a single PDF.

    Returns (output_md_path, stats_dict).
    """
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict
    from marker.output import text_from_rendered

    t_start = time.monotonic()

    converter = PdfConverter(
        artifact_dict=create_model_dict(),
    )
    rendered = converter(str(pdf_path))

    text, metadata, images = text_from_rendered(rendered)
    elapsed = time.monotonic() - t_start

    # Write markdown
    out_dir = output_dir or OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{pdf_path.stem}.md"
    md_path.write_text(text, encoding="utf-8")

    # Build stats
    stats = {
        "pdf": str(pdf_path),
        "pages": rendered.metadata.get("total_pages", 0),
        "time_s": elapsed,
        "output_size": len(text),
        "sections": _count_sections(text),
        "formulas": _count_formulas(text),
        "tables": _count_tables(text),
        "images": len(images),
    }

    # Write stats JSON
    stats_path = out_dir / f"{pdf_path.stem}_marker_stats.json"
    stats_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")

    print(
        f"  {pdf_path.name}: {elapsed:.1f}s, {len(text)} chars, "
        f"{stats['sections']} sections, {stats['formulas']} formulas, {len(images)} imgs"
    )

    return md_path, stats


def _count_sections(text: str) -> int:
    """Count markdown heading lines."""
    count = 0
    for line in text.split("\n"):
        if line.startswith("#") and not line.startswith("####"):
            count += 1
    return count


def _count_formulas(text: str) -> int:
    """Count LaTeX formula blocks."""
    return text.count("$$") // 2 + text.count("$ ")  # rough


def _count_tables(text: str) -> int:
    """Count markdown tables."""
    lines = text.split("\n")
    count = 0
    for i, line in enumerate(lines):
        if i > 0 and "|---" in line and "|" in lines[i - 1]:
            count += 1
    return count


# ============================================================
# 对比 MinerU
# ============================================================
def compare_with_mineru(pdf_path: Path, marker_md_path: Path) -> dict:
    """Compare Marker output with existing MinerU output."""
    mineru_md = DATA_DIR / f"{pdf_path.stem}.md"
    mineru_json = DATA_DIR / f"{pdf_path.stem}_content_list.json"

    result = {
        "paper": pdf_path.stem,
        "mineru_exists": mineru_md.exists(),
    }

    if not mineru_md.exists():
        result["note"] = "No MinerU output found"
        return result

    mineru_text = mineru_md.read_text(encoding="utf-8")
    marker_text = marker_md_path.read_text(encoding="utf-8")

    # Basic stats
    result["mineru_chars"] = len(mineru_text)
    result["marker_chars"] = len(marker_text)
    result["mineru_sections"] = _count_sections(mineru_text)
    result["marker_sections"] = _count_sections(marker_text)
    result["mineru_formulas"] = _count_formulas(mineru_text)
    result["marker_formulas"] = _count_formulas(marker_text)

    # MinerU content_list stats
    if mineru_json.exists():
        cl = json.loads(mineru_json.read_text(encoding="utf-8"))
        result["mineru_blocks"] = len(cl)
        types = {}
        for item in cl:
            t = item.get("type", "unknown")
            types[t] = types.get(t, 0) + 1
        result["mineru_block_types"] = types

    return result


# ============================================================
# Quick mode: 单篇 API 调用 + 侧对比
# ============================================================
def cmd_quick(pdf_name: str = "2601.15307v1.pdf"):
    """Run Marker on ONE paper via Python API and show side-by-side with MinerU."""
    pdf_path = DATA_DIR / pdf_name
    if not pdf_path.exists():
        print(f"❌ PDF not found: {pdf_path}")
        sys.exit(1)

    print(f"📄 单篇快速测试: {pdf_name}")
    print(f"   GPU: {_gpu_info()}")
    print()

    md_path, stats = run_marker_single(pdf_path)
    print()

    # Comparison
    comparison = compare_with_mineru(pdf_path, md_path)
    _print_comparison(comparison)


def cmd_quick_batch(pdf_names: list[str] | None = None):
    """Run Marker on a few papers via API."""
    if pdf_names:
        pdf_paths = [DATA_DIR / n for n in pdf_names]
    else:
        pdf_paths = sorted(DATA_DIR.glob("*.pdf"))[:3]  # first 3

    print(f"📄 批量快速测试: {len(pdf_paths)} PDFs")
    print(f"   GPU: {_gpu_info()}")
    print()

    all_stats = []
    for pdf_path in pdf_paths:
        print(f"  📄 {pdf_path.name} ...")
        try:
            md_path, stats = run_marker_single(pdf_path)
            all_stats.append(stats)
        except Exception as e:
            print(f"     ❌ Failed: {e}")
            all_stats.append({"pdf": str(pdf_path), "error": str(e)})
        print()

    total_time = sum(s.get("time_s", 0) for s in all_stats)
    total_chars = sum(s.get("output_size", 0) for s in all_stats)
    print(f"✅ 总计: {total_time:.1f}s, {total_chars} chars")


def cmd_all(max_pages: int | None = None, workers: int = 4):
    """Run Marker on ALL test PDFs using batch CLI."""
    pdf_paths = sorted(DATA_DIR.glob("*.pdf"))

    print(f"📚 全量测试: {len(pdf_paths)} PDFs, {workers} workers")
    print(f"   GPU: {_gpu_info()}")
    print()

    stats = run_marker_batch(pdf_paths, workers=workers, max_pages=max_pages)

    if stats["status"] != "ok":
        return

    # Compare all
    print()
    print("=" * 60)
    print("  vs MinerU 对比总览")
    print("=" * 60)
    print(f"{'Paper':<30} {'MinerU':>10} {'Marker':>10} {'Sections':>12} {'Formulas':>12}")
    print("-" * 60)

    for pdf_path in pdf_paths:
        marker_md = OUTPUT_DIR / f"{pdf_path.stem}.md"
        if marker_md.exists():
            c = compare_with_mineru(pdf_path, marker_md)
            print(
                f"{c['paper']:<30} "
                f"{c.get('mineru_chars', 0):>10,} "
                f"{c.get('marker_chars', 0):>10,} "
                f"{c.get('mineru_sections', 0)}/{c.get('marker_sections', 0):>3} "
                f"{c.get('mineru_formulas', 0)}/{c.get('marker_formulas', 0):>3}"
            )

    print("-" * 60)
    compare_json = OUTPUT_DIR / "comparison.json"
    all_c = [
        compare_with_mineru(p, OUTPUT_DIR / f"{p.stem}.md")
        for p in pdf_paths
        if (OUTPUT_DIR / f"{p.stem}.md").exists()
    ]
    compare_json.write_text(json.dumps(all_c, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"   详细对比: {compare_json}")


# ============================================================
# Helpers
# ============================================================
def _gpu_info() -> str:
    try:
        import torch

        if torch.cuda.is_available():
            n = torch.cuda.device_count()
            names = [torch.cuda.get_device_name(i).replace("NVIDIA ", "") for i in range(n)]
            mems = [
                f"{torch.cuda.get_device_properties(i).total_memory / 1e9:.0f}GB" for i in range(n)
            ]
            return f"{n}x {names[0]} ({', '.join(mems)})"
    except Exception:
        pass
    return "No GPU / unknown"


def _print_comparison(c: dict):
    print("=" * 60)
    print(f"  {c['paper']}  —  Marker vs MinerU")
    print("=" * 60)
    if c.get("note"):
        print(f"  ⚠️  {c['note']}")
        return
    print(f"  {'':20} {'MinerU':>12} {'Marker':>12}")
    print(f"  {'Chars':20} {c.get('mineru_chars', 0):>12,} {c.get('marker_chars', 0):>12,}")
    print(
        f"  {'Sections (#+)':20} {c.get('mineru_sections', 0):>12} {c.get('marker_sections', 0):>12}"
    )
    print(
        f"  {'Formulas ($$)':20} {c.get('mineru_formulas', 0):>12} {c.get('marker_formulas', 0):>12}"
    )
    if c.get("mineru_blocks"):
        print(f"  {'Content blocks':20} {c['mineru_blocks']:>12} {'N/A':>12}")
        for t, n in c.get("mineru_block_types", {}).items():
            print(f"    - {t}: {n}")


# ============================================================
# CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="Marker PDF parsing benchmark — run on GPU notebook (4x4090)"
    )
    parser.add_argument("--install", action="store_true", help="Install Marker with GPU support")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Run Marker on ONE paper via Python API + compare with MinerU",
    )
    parser.add_argument(
        "--quick-batch",
        type=int,
        default=None,
        metavar="N",
        help="Run Marker on first N papers via Python API",
    )
    parser.add_argument("--all", action="store_true", help="Run Marker batch CLI on ALL 8 papers")
    parser.add_argument("--max-pages", type=int, default=None, help="Max pages per PDF (for --all)")
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of worker processes (default: 8, two per GPU on 4x4090)",
    )
    parser.add_argument(
        "--pdf", type=str, default=None, nargs="*", help="Specific PDF file(s) to process"
    )
    args = parser.parse_args()

    if args.install:
        return cmd_install()

    if args.quick:
        return cmd_quick()

    if args.quick_batch:
        return cmd_quick_batch()

    if args.all or args.pdf:
        pdf_names = args.pdf if args.pdf else None
        return cmd_all(max_pages=args.max_pages, workers=args.workers)

    # Default: quick mode
    print("No action specified. Use --quick, --quick-batch N, --all, or --install")
    print("💡 Running default: --quick (single paper test)")
    print()
    return cmd_quick()


if __name__ == "__main__":
    main()
