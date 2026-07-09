#!/usr/bin/env python3
"""test_extract_pipeline.py — 小批量测试 LaTeX / PDF → Markdown 管线。

采样策略:
  只取 has_latex == true & has_pdf == true 的论文，同一篇既走 LaTeX 又走 PDF，
  产出放在同一目录下方便对比。

输出:
  data/extract_test_20260604/{arxiv_id}/
    ├── latex.md    # latex_to_markdown() 结果
    └── pdf.md      # pdf_to_markdown() 结果

用法:
  python scripts/test_extract_pipeline.py               # 默认: 20 篇
  python scripts/test_extract_pipeline.py -n 100         # 100 篇
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import structlog

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scholight.logging import configure_logging
from scholight.pipeline.latex_md import LatexMdError, latex_to_markdown
from scholight.pipeline.pdf_md import PDFMdError, pdf_to_markdown
from scholight.storage import storage
from scholight.store.client import QUERY_CONSISTENCY, get_client

logger = structlog.get_logger(__name__)

# ── CLI ────────────────────────────────────────────────────────────────────────
_parser = argparse.ArgumentParser(description="小批量对比测试 LaTeX / PDF → Markdown")
_parser.add_argument("-n", type=int, default=20, help="采样数 (default: 20)")
_parser.add_argument("--log-level", type=str, default="INFO")
_args = _parser.parse_args()

DATA_ROOT = Path(
    os.environ.get(
        "SCHOLIGHT_DATA_ROOT",
        "/inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/academic-data",
    )
)
TEST_DIR = Path(__file__).resolve().parents[1] / "data" / "extract_test_20260604"


def main() -> None:
    configure_logging(log_level=_args.log_level, use_json=False)
    TEST_DIR.mkdir(parents=True, exist_ok=True)

    # ── 采样：同时有 LaTeX 源和 PDF 的论文 ───────────────────────
    client = get_client()
    logger.info("sampling papers with both has_latex and has_pdf ...")

    rows = client.query(
        "arxiv_papers",
        filter="has_latex == True and has_pdf == True",
        output_fields=["arxiv_id", "created"],
        consistency_level=QUERY_CONSISTENCY,
        limit=_args.n * 4,  # oversample to handle missing files
    )
    logger.info("candidates", count=len(rows))

    # ── 逐个对比提取 ──────────────────────────────────────────────
    latex_ok = 0
    latex_fail = 0
    latex_skip = 0
    pdf_ok = 0
    pdf_fail = 0
    pdf_skip = 0
    done = 0

    for row in rows:
        if done >= _args.n:
            break
        aid = row["arxiv_id"]
        created = row.get("created", "")
        if not created:
            continue

        paper_dir = TEST_DIR / aid.replace("/", "_")
        latex_path = storage.latex_dir(aid, created)
        pdf_path = storage.pdf_path(aid, created)

        # 两个资源都存在才处理
        if not latex_path.is_dir():
            latex_skip += 1
            continue
        if not pdf_path.is_file():
            pdf_skip += 1
            continue

        paper_dir.mkdir(parents=True, exist_ok=True)
        done += 1
        prefix = f"[{done}/{_args.n}] {aid}"

        # ── LaTeX 路线 ──
        try:
            t0 = time.monotonic()
            md = latex_to_markdown(latex_path)
            elapsed = time.monotonic() - t0
            (paper_dir / "latex.md").write_text(md, encoding="utf-8")
            latex_ok += 1
            logger.info(f"{prefix}  latex ✅ {len(md):,} chars in {elapsed * 1000:.0f}ms")
        except (LatexMdError, FileNotFoundError) as exc:
            latex_fail += 1
            logger.warning(f"{prefix}  latex ❌ {exc}")
        except Exception:
            latex_fail += 1
            logger.exception(f"{prefix}  latex 💥")

        # ── PDF 路线 ──
        try:
            t0 = time.monotonic()
            md = pdf_to_markdown(pdf_path)
            elapsed = time.monotonic() - t0
            (paper_dir / "pdf.md").write_text(md, encoding="utf-8")
            pdf_ok += 1
            logger.info(f"{prefix}  pdf   ✅ {len(md):,} chars in {elapsed * 1000:.0f}ms")
        except (PDFMdError, FileNotFoundError) as exc:
            pdf_fail += 1
            logger.warning(f"{prefix}  pdf   ❌ {exc}")
        except Exception:
            pdf_fail += 1
            logger.exception(f"{prefix}  pdf   💥")

    # ── 汇总 ──────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"  Side-by-Side Test Results — {datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'=' * 60}")
    print(f"  对比论文数: {done}")
    print()
    print("  LaTeX 路线 (latex_to_markdown):")
    print(f"    ✅ {latex_ok:>3}   ❌ {latex_fail:>3}   ⏭ {latex_skip:>3}")
    print()
    print("  PDF   路线 (pdf_to_markdown):")
    print(f"    ✅ {pdf_ok:>3}   ❌ {pdf_fail:>3}   ⏭ {pdf_skip:>3}")
    print()
    print(f"  📂 output: {TEST_DIR}")
    print("     每篇 = {id}/latex.md + {id}/pdf.md")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
