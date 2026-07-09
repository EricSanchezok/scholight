#!/usr/bin/env python3
"""Test PaddleOCR-VL-1.5 via SiliconFlow API on arXiv PDFs.

Converts PDF pages to images, sends to PaddleOCR-VL-1.5 via OpenAI-compatible
API, and saves markdown + raw JSON output for comparison with MinerU results.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import fitz  # PyMuPDF
from openai import OpenAI

SILICONFLOW_BASE = "https://api.siliconflow.cn/v1"
MODEL_NAME = "PaddlePaddle/PaddleOCR-VL-1.5"

# PaddleOCR-VL uses specific trigger prompts per task mode.
# "OCR:" = document text recognition (clean text, no markdown)
# For structured markdown we use a descriptive prompt that
# instructs the model to output in our desired format.
TASK_PROMPTS = {
    "ocr": "OCR:",
    "table": "Table Recognition:",
    "formula": "Formula Recognition:",
    "chart": "Chart Recognition:",
    "spotting": "Spotting:",
    "seal": "Seal Recognition:",
}
TASK = "ocr"

# Use a descriptive prompt instead of just "OCR:" to get structured output.
STRUCTURED_PROMPT = (
    "OCR with markdown formatting. Output as clean markdown:\n"
    "- Section headings as # / ## / ###\n"
    "- Formulas as LaTeX $$...$$ or $...$\n"
    "- Tables as markdown tables\n"
    "- Figures as [Image: description]\n"
    "- Remove headers/footers/page numbers\n"
)


def pdf_page_to_b64(page: fitz.Page, dpi: int = 200) -> tuple[str, int, int]:
    """Render a single PDF page to a base64-encoded PNG string."""
    pix = page.get_pixmap(dpi=dpi)
    img_bytes = pix.tobytes("png")
    b64_str = base64.b64encode(img_bytes).decode("utf-8")
    return b64_str, pix.width, pix.height


def call_siliconflow(client: OpenAI, image_b64: str, task: str = TASK) -> tuple[str, int, int]:
    """Send a single page image to SiliconFlow PaddleOCR-VL-1.5 API.

    Uses PaddleOCR-VL's native task-specific trigger prompt (e.g. "OCR:")
    rather than a generic system prompt, to activate the correct parsing mode.

    Returns (markdown_text, prompt_tokens, completion_tokens).
    """
    resp = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{image_b64}",
                        },
                    },
                    {
                        "type": "text",
                        "text": TASK_PROMPTS.get(task, TASK_PROMPTS["ocr"])
                        + "\n"
                        + STRUCTURED_PROMPT,
                    },
                ],
            },
        ],
        max_tokens=8192,
        temperature=0.1,
    )

    content = resp.choices[0].message.content or ""
    pt = resp.usage.prompt_tokens if resp.usage else 0
    ct = resp.usage.completion_tokens if resp.usage else 0
    return content, pt, ct


def parse_pdf(
    pdf_path: Path,
    client: OpenAI,
    output_dir: Path,
    start_page: int = 0,
    max_pages: int | None = None,
    page_delay: float = 1.0,
) -> dict:
    """Parse a single PDF file page by page.

    Returns a dict with parsing stats.
    """
    doc = fitz.open(str(pdf_path))
    total_pages = len(doc)
    end_page = min(total_pages, start_page + max_pages) if max_pages else total_pages
    pages_to_process = range(start_page, end_page)

    arxiv_id = pdf_path.stem
    output_dir.mkdir(parents=True, exist_ok=True)

    stats = {
        "arxiv_id": arxiv_id,
        "total_pages": total_pages,
        "processed_pages": 0,
        "failed_pages": 0,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_time_s": 0.0,
        "per_page_times": [],
        "per_page_tokens": [],
    }

    all_md_parts = []
    t_start = time.monotonic()

    for page_idx in pages_to_process:
        page_start = time.monotonic()
        try:
            page = doc[page_idx]
            img_b64, w, h = pdf_page_to_b64(page, dpi=200)
            md_text, pt, ct = call_siliconflow(client, img_b64)

            elapsed = time.monotonic() - page_start
            stats["processed_pages"] += 1
            stats["total_prompt_tokens"] += pt
            stats["total_completion_tokens"] += ct
            stats["per_page_times"].append(elapsed)
            stats["per_page_tokens"].append({"prompt": pt, "completion": ct, "total": pt + ct})

            all_md_parts.append(f"<!-- PAGE {page_idx + 1} -->\n\n{md_text}")
            print(f"  Page {page_idx + 1}/{total_pages}: {elapsed:.1f}s, {pt}+{ct} tokens")
        except Exception as exc:
            stats["failed_pages"] += 1
            all_md_parts.append(f"<!-- PAGE {page_idx + 1} FAILED: {exc} -->\n\n")
            print(f"  Page {page_idx + 1}/{total_pages}: FAILED - {exc}")

        # Rate-limit friendly delay between pages
        if page_idx < end_page - 1:
            time.sleep(page_delay)

    doc.close()
    t_total = time.monotonic() - t_start
    stats["total_time_s"] = t_total

    # Write markdown
    full_md = "\n\n".join(all_md_parts)
    md_path = output_dir / f"{arxiv_id}.md"
    md_path.write_text(full_md, encoding="utf-8")
    print(f"  → {md_path} ({len(full_md)} chars)")

    # Write stats
    stats_path = output_dir / f"{arxiv_id}_stats.json"
    stats_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  → {stats_path}")

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test PaddleOCR-VL-1.5 via SiliconFlow API on arXiv PDFs"
    )
    parser.add_argument(
        "--pdf-dir",
        default="data",
        help="Directory containing PDF files to parse (default: data/)",
    )
    parser.add_argument(
        "--output-dir",
        default="data/paddleocr_output",
        help="Output directory for markdown and stats (default: data/paddleocr_output)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("SILICONFLOW_API_KEY", ""),
        help="SiliconFlow API key (default: $SILICONFLOW_API_KEY env var)",
    )
    parser.add_argument(
        "--pdf",
        default=None,
        nargs="*",
        help="Specific PDF file(s) to process (default: all .pdf in --pdf-dir)",
    )
    parser.add_argument(
        "--start-page",
        type=int,
        default=0,
        help="Start from this page (0-indexed, default: 0)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Maximum pages to process per PDF (default: all)",
    )
    parser.add_argument(
        "--page-delay",
        type=float,
        default=1.0,
        help="Delay in seconds between page API calls (default: 1.0)",
    )
    parser.add_argument(
        "--single-page",
        type=int,
        default=None,
        help="Process only this specific page number (0-indexed) for each PDF",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=200,
        help="DPI for PDF→image rendering (default: 200)",
    )
    args = parser.parse_args()

    pdf_dir = Path(args.pdf_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    client = OpenAI(
        base_url=SILICONFLOW_BASE,
        api_key=args.api_key,
    )

    # Discover PDFs
    if args.pdf:
        pdf_paths = [Path(p) if Path(p).is_absolute() else pdf_dir / p for p in args.pdf]
    else:
        pdf_paths = sorted(pdf_dir.glob("*.pdf"))

    if not pdf_paths:
        print("No PDF files found!", file=sys.stderr)
        sys.exit(1)

    print(f"Processing {len(pdf_paths)} PDF(s) via SiliconFlow {MODEL_NAME}")
    print(f"Pages: {'single page' if args.single_page is not None else 'all pages'}")
    print(f"DPI: {args.dpi}")
    print()

    all_stats = []
    grand_start = time.monotonic()

    for pdf_path in pdf_paths:
        print(f"📄 {pdf_path.name} ...")
        stats = parse_pdf(
            pdf_path=pdf_path,
            client=client,
            output_dir=output_dir,
            start_page=args.single_page if args.single_page is not None else args.start_page,
            max_pages=1 if args.single_page is not None else args.max_pages,
            page_delay=args.page_delay,
        )
        all_stats.append(stats)
        print()

    grand_total = time.monotonic() - grand_start

    # Summary
    total_pages = sum(s["processed_pages"] for s in all_stats)
    total_failed = sum(s["failed_pages"] for s in all_stats)
    total_pt = sum(s["total_prompt_tokens"] for s in all_stats)
    total_ct = sum(s["total_completion_tokens"] for s in all_stats)

    print("=" * 60)
    print("SUMMARY")
    print(f"  PDFs:        {len(all_stats)}")
    print(f"  Pages OK:    {total_pages}")
    print(f"  Pages FAIL:  {total_failed}")
    print(f"  Wall time:   {grand_total:.1f}s")
    print(f"  Tokens:      {total_pt} prompt + {total_ct} completion")
    if total_pages > 0:
        print(f"  Avg/page:    {grand_total / total_pages:.1f}s")
    print(f"  Output:      {output_dir}/")


if __name__ == "__main__":
    main()
