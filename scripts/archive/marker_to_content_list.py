#!/usr/bin/env python3
"""
Marker → MinerU 格式转换器

将 Marker 内部 Block 结构导出为与 MinerU 兼容的 _content_list.json：
  [
    {"type": "text", "text": "...", "text_level": 1, "bbox": [x0,y0,x1,y1], "page_idx": 0},
    {"type": "image", "bbox": [...], "page_idx": 0},
    {"type": "table", "bbox": [...], "page_idx": 0},
    ...
  ]

用法:
    python scripts/marker_to_content_list.py data/2601.15307v1.pdf
    python scripts/marker_to_content_list.py data/2601.15307v1.pdf --output data/marker_output/
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from marker.converters.pdf import PdfConverter
from marker.models import create_model_dict

from compass.utils.marker import marker_block_to_content


def dump_content_list(pdf_path: Path, output_dir: Path) -> tuple[Path, int]:
    """Parse a PDF with Marker and dump _content_list.json + .md."""
    print(f"📄 {pdf_path.name} ...")
    t0 = time.monotonic()

    from marker.output import text_from_rendered

    converter = PdfConverter(artifact_dict=create_model_dict())

    # build_document() returns the Document with full page/block structure
    document = converter.build_document(str(pdf_path))

    # Render through the converter's renderer to get markdown
    renderer = converter.resolve_dependencies(converter.renderer)
    rendered = renderer(document)
    text, _, _ = text_from_rendered(rendered)

    # Walk all pages, extract blocks
    content_list = []
    for page_idx, page in enumerate(document.pages):
        for child in page.current_children:
            item = marker_block_to_content(child, document, page_idx)
            if item:
                content_list.append(item)

    elapsed = time.monotonic() - t0

    output_dir.mkdir(parents=True, exist_ok=True)

    # Write _content_list.json
    json_path = output_dir / f"{pdf_path.stem}_content_list.json"
    json_path.write_text(json.dumps(content_list, indent=2, ensure_ascii=False), encoding="utf-8")

    # Write .md
    md_path = output_dir / f"{pdf_path.stem}.md"
    md_path.write_text(text, encoding="utf-8")

    # Summary stats
    types = {}
    for item in content_list:
        t = item.get("type", "unknown")
        types[t] = types.get(t, 0) + 1

    print(f"  ✅ {elapsed:.1f}s | {len(content_list)} blocks | {json_path}")
    print(f"     types: {types}")
    return json_path, len(content_list)


def cmd_test_one():
    """Quick test: parse 2601.15307v1 and compare with MinerU JSON."""
    from pathlib import Path

    project = Path(__file__).resolve().parent.parent
    pdf_path = project / "data" / "2601.15307v1.pdf"
    output_dir = project / "data" / "marker_output"

    json_path, count = dump_content_list(pdf_path, output_dir)

    # Side-by-side with MinerU
    mineru_json = project / "data" / "2601.15307v1_content_list.json"
    if mineru_json.exists():
        mineru = json.loads(mineru_json.read_text())
        marker = json.loads(json_path.read_text())

        m_types = {}
        for item in mineru:
            m_types[item.get("type", "?")] = m_types.get(item.get("type", "?"), 0) + 1
        mk_types = {}
        for item in marker:
            mk_types[item.get("type", "?")] = mk_types.get(item.get("type", "?"), 0) + 1

        print("\n" + "=" * 50)
        print("  MinerU vs Marker content_list comparison")
        print("=" * 50)
        print(f"  {'':15} {'MinerU':>10} {'Marker':>10}")
        print(f"  {'blocks':15} {len(mineru):>10} {count:>10}")
        all_keys = sorted(set(m_types) | set(mk_types))
        for k in all_keys:
            print(f"  {'  ' + k:15} {m_types.get(k, 0):>10} {mk_types.get(k, 0):>10}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Marker → MinerU content_list.json converter")
    parser.add_argument("pdf", nargs="*", help="PDF files to process")
    parser.add_argument("--output", "-o", default=None, help="Output directory")
    parser.add_argument("--test", action="store_true", help="Quick test with built-in comparison")
    args = parser.parse_args()

    if args.test:
        cmd_test_one()
        sys.exit(0)

    project = Path(__file__).resolve().parent.parent
    pdf_dir = project / "data"
    output_dir = Path(args.output) if args.output else (project / "data" / "marker_output")

    if args.pdf:
        pdfs = [(Path(p) if Path(p).is_absolute() else pdf_dir / p) for p in args.pdf]
    else:
        pdfs = sorted(pdf_dir.glob("*.pdf"))

    print(f"Marker → _content_list.json: {len(pdfs)} PDFs\n")

    for pdf_path in pdfs:
        if not pdf_path.exists():
            print(f"  ❌ Not found: {pdf_path}")
            continue
        try:
            dump_content_list(pdf_path, output_dir)
        except Exception as e:
            print(f"  ❌ {pdf_path.name}: {e}")
