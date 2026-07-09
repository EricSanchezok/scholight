#!/usr/bin/env python3
"""sync_arxiv.py — 补齐 2025-09 之后的新论文全文，并支持每日增量。

数据流:
  OAI-PMH 发现新论文 → 检查 parquet 去重 → 下载 PDF
  → MinerU API 解析 (parser.py) → 写入 parquet

parquet 格式与 parse_arxiv.py 完全对齐:
  schema: {
      "content_list": JSON string (per-page MinerU block list),
      "figure_images": "[]"
  }
  路径:  OUTPUT_DIR/YYYY/MM/{arxiv_id}.parquet

PDF 存储: PDF_DIR/YYYY/MM/{arxiv_id}.pdf  (与 parquet 同目录体系)

用法:
  # 一次性补齐: 2025-09-01 → 今天
  python scripts/sync_arxiv.py --backfill

  # 每日增量: 昨天的新论文
  python scripts/sync_arxiv.py --daily

  # 指定日期范围
  python scripts/sync_arxiv.py --from 2026-01-01 --to 2026-01-31

  # 预览模式(不实际下载解析)
  python scripts/sync_arxiv.py --backfill --dry-run

  # 从 OAI-PMH resumption token 续传
  python scripts/sync_arxiv.py --resume TOKEN_STRING

环境变量:
  COMPASS_MINERU_API_KEY   MinerU API key
  OUTPUT_DIR               parquet 输出目录 (与 parse_arxiv.py 共用)
  PDF_DIR                  PDF 下载目录 (默认 OUTPUT_DIR/../pdfs)
  COMPASS_LOG_LEVEL        日志级别 (默认 INFO)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

# ── Paths ───────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from compass.config import settings  # noqa: E402
from compass.logging import configure_logging  # noqa: E402

configure_logging(log_level=settings.log_level)

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/data/output/parsed"))
PDF_DIR = Path(os.environ.get("PDF_DIR", str(OUTPUT_DIR.parent / "pdfs")))

OAI_BASE = "https://oaipmh.arxiv.org/oai"
ARXIV_PDF_BASE = "https://arxiv.org/pdf"
ARXIV_E_PRINT_PDF = "https://arxiv.org/e-print"  # for very recent papers

CHECKPOINT_FILE = OUTPUT_DIR / "checkpoints" / "sync_checkpoint.json"
_CP_INTERVAL = 100  # write checkpoint every N papers

log = structlog.get_logger("sync-arxiv")

# OAI-PMH resumption token regex for recovery
_RESUMPTION_RE = re.compile(r"<resumptionToken[^>]*>(.*?)</resumptionToken>", re.DOTALL)
_LISTRECORDS_RE = re.compile(r"<ListRecords>(.*?)</ListRecords>", re.DOTALL)
_RECORD_RE = re.compile(r"<record>.*?</record>", re.DOTALL)

# ── OAI-PMH client ─────────────────────────────────────────────────────


class OAIHarvestError(Exception):
    """OAI-PMH harvesting failed."""


def _oai_url(verb: str, metadata_prefix: str = "arXivRaw", **params: str) -> str:
    extra = urlencode(params) if params else ""
    return (
        f"{OAI_BASE}?verb={verb}&metadataPrefix={metadata_prefix}{('&' + extra) if extra else ''}"
    )


def _oai_resume_url(token: str) -> str:
    """Build OAI-PMH URL for a resumptionToken — no other params allowed."""
    return f"{OAI_BASE}?verb=ListRecords&resumptionToken={token}"


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=5, max=120),
    retry=retry_if_exception_type((httpx.HTTPError, OAIHarvestError)),
    reraise=True,
)
def _fetch_oai_page(url: str) -> str:
    resp = httpx.get(url, timeout=30, follow_redirects=True)
    # Handle HTTP 503 — arXiv throttle signal with Retry-After
    if resp.status_code == 503:
        retry_after = resp.headers.get("Retry-After", "20")
        try:
            wait_sec = int(retry_after)
        except ValueError:
            wait_sec = 20
        wait_sec = int(wait_sec * 1.5)
        log.warning("OAI-PMH 503 throttle", retry_after=retry_after, wait_sec=wait_sec)
        time.sleep(wait_sec)
        raise OAIHarvestError(f"HTTP 503 — retry after {wait_sec}s")
    resp.raise_for_status()
    body = resp.text
    if "<error" in body:
        code = re.search(r'<error[^>]*code="([^"]*)"', body)
        msg = re.search(r"<error[^>]*>([^<]*)</error>", body)
        raise OAIHarvestError(
            f"OAI-PMH error {code.group(1) if code else '?'}: {msg.group(1) if msg else '?'}"
        )
    return body


def iter_papers_oai(from_date: str, until_date: str, resume_token: str = "") -> list[dict]:
    """Yield paper metadata from OAI-PMH ListRecords.

    Each paper is a dict: arxiv_id, title, abstract, authors, categories,
    created, updated, license, comments, doi, journal_ref, acm_class,
    pdf_url.

    Handles resumptionToken for large result sets.
    """
    papers: list[dict] = []
    if resume_token:
        url = _oai_resume_url(resume_token)
    else:
        url = _oai_url("ListRecords", **{"from": from_date, "until": until_date})
    body = _fetch_oai_page(url)

    # Extract all <record> blocks
    records = _RECORD_RE.findall(body)
    for rec in records:
        paper = _parse_record(rec)
        if paper:
            papers.append(paper)

    # Check for resumption token
    rt_match = _RESUMPTION_RE.search(body)
    token_text = rt_match.group(1).strip() if rt_match else ""
    if token_text and token_text != resume_token:
        log.info("resumption token found, fetching next page", token=token_text[:40])
        time.sleep(10)  # OAI-PMH polite delay: 10-15s between resumptionToken requests
        papers.extend(iter_papers_oai(from_date, until_date, resume_token=token_text))

    return papers


def _parse_record(record_xml: str) -> dict | None:
    """Parse one OAI-PMH arXivRaw <record> into a paper dict.

    arXivRaw uses flat author strings ("Name1 and Name2") and version dates.
    created = <header><datestamp>, updated = latest <version><date>.

    Returns None on parse failure.
    """
    # Extract arxiv_id from <header><identifier>oai:arXiv.org:XXXX.XXXXX
    id_match = re.search(r"<identifier>oai:arXiv\.org:(.*?)</identifier>", record_xml)
    if not id_match:
        return None
    arxiv_id = id_match.group(1).strip()

    def _tag(name: str, default: str = "") -> str:
        m = re.search(f"<{name}>(.*?)</{name}>", record_xml, re.DOTALL)
        return m.group(1).strip() if m else default

    # Authors: arXivRaw has flat strings separated by " and "
    def _authors_raw() -> list[str]:
        raw = _tag("authors")
        if not raw:
            return []
        return [a.strip() for a in re.split(r"\s+and\s+", raw) if a.strip()]

    # created = datestamp from <header>
    created = _tag("datestamp", "")
    # updated = most recent <version><date>
    version_dates = re.findall(r"<date>(.*?)</date>", record_xml)
    updated = version_dates[-1] if version_dates else created

    # Detect PDF URL: recent papers (post-2025) use arxiv.org/abs/YYMM.NNNNN
    # Some very recent may only be available via e-print
    pdf_url = f"{ARXIV_PDF_BASE}/{arxiv_id}.pdf"

    return {
        "arxiv_id": arxiv_id,
        "title": _tag("title"),
        "abstract": _tag("abstract"),
        "authors": _authors_raw(),
        "categories": _tag("categories", "").split(),
        "created": created,
        "updated": updated,
        "license": _tag("license", ""),
        "comments": _tag("comments", ""),
        "doi": _tag("doi", ""),
        "journal_ref": _tag("journal-ref", ""),
        "acm_class": _tag("msc-class", "") or _tag("acm-class", ""),
        "pdf_url": pdf_url,
    }


# ── Parquet output (matches parse_arxiv.py format exactly) ──────────────


def arxiv_to_path(arxiv_id: str) -> tuple[int, int, Path]:
    """arxiv_id → (year, month, parquet_path).

    "2501.00001" → (2025, 1, OUTPUT_DIR/2025/01/2501.00001.parquet)
    """
    from compass.utils import parse_arxiv_id

    year, month = parse_arxiv_id(arxiv_id)
    out_dir = OUTPUT_DIR / str(year) / f"{month:02d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    return year, month, out_dir / f"{arxiv_id}.parquet"


def pdf_path_for(arxiv_id: str) -> Path:
    """Return the PDF storage path: PDF_DIR/YYYY/MM/{arxiv_id}.pdf"""
    from compass.utils import parse_arxiv_id

    year, month = parse_arxiv_id(arxiv_id)
    out_dir = PDF_DIR / str(year) / f"{month:02d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{arxiv_id}.pdf"


def save_parquet(arxiv_id: str, content_list: list) -> Path:
    """Write content_list as parquet — same schema as parse_arxiv.py."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    _, _, path = arxiv_to_path(arxiv_id)
    table = pa.table(
        {
            "content_list": [
                json.dumps(content_list, ensure_ascii=False) if content_list else "[]"
            ],
            "figure_images": ["[]"],
        }
    )
    pq.write_table(table, path, compression="zstd")
    return path


def parquet_exists(arxiv_id: str) -> bool:
    """Check if parquet already exists for this arxiv_id."""
    try:
        _, _, path = arxiv_to_path(arxiv_id)
    except ValueError:
        return False
    return path.exists()


# ── PDF download ────────────────────────────────────────────────────────


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type((httpx.HTTPError, OSError)),
    reraise=True,
)
def download_pdf(arxiv_id: str, url: str) -> Path:
    """Download PDF, return local path."""
    dest = pdf_path_for(arxiv_id)
    if dest.exists():
        log.debug("pdf already downloaded, skipping", arxiv_id=arxiv_id)
        return dest

    resp = httpx.get(url, timeout=60, follow_redirects=True)
    resp.raise_for_status()

    # Check it's actually a PDF
    ct = resp.headers.get("content-type", "")
    if "pdf" not in ct.lower() and not url.endswith(".pdf"):
        log.warning("response may not be pdf", arxiv_id=arxiv_id, content_type=ct)

    dest.write_bytes(resp.content)
    log.debug("pdf downloaded", arxiv_id=arxiv_id, size_kb=len(resp.content) // 1024)
    return dest


# ── MinerU parse wrapper ────────────────────────────────────────────────


def parse_with_mineru_api(pdf_path: Path, arxiv_id: str) -> list[dict]:
    """Call MinerU API to parse PDF → content_list."""
    from compass.pipeline.parser import parse_pdf as _parse_pdf

    tmp_dir = OUTPUT_DIR / ".tmp_mineru"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    cl_path = _parse_pdf(str(pdf_path), str(tmp_dir), arxiv_id=arxiv_id)
    return json.loads(cl_path.read_text(encoding="utf-8"))


# ── Checkpoint ──────────────────────────────────────────────────────────


def load_checkpoint() -> dict:
    """Load checkpoint dict or return empty default."""
    if CHECKPOINT_FILE.exists():
        return json.loads(CHECKPOINT_FILE.read_text())
    return {"processed": [], "errors": [], "total_seen": 0, "resume_token": ""}


def save_checkpoint(state: dict) -> None:
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.write_text(json.dumps(state, ensure_ascii=False))


# ── Main pipeline ───────────────────────────────────────────────────────


async def process_papers(papers: list[dict], dry_run: bool = False) -> dict:
    """Process a list of paper metadata: download → parse → save.

    Returns {"ok": N, "skip": N, "fail": N}.
    """
    cp = load_checkpoint()
    processed = set(cp.get("processed", []))
    ok = skip = fail = 0

    for paper in papers:
        aid = paper["arxiv_id"]

        # Skip already processed
        if aid in processed:
            skip += 1
            continue

        # Skip if parquet already exists
        if parquet_exists(aid):
            processed.add(aid)
            skip += 1
            continue

        if dry_run:
            log.info("dry-run: would process", arxiv_id=aid)
            ok += 1
            processed.add(aid)
            continue

        try:
            # 1. Download PDF
            log.debug("downloading pdf", arxiv_id=aid)
            pdf = download_pdf(aid, paper["pdf_url"])

            # 2. Parse with MinerU API
            log.debug("parsing with mineru", arxiv_id=aid)
            content_list = parse_with_mineru_api(pdf, aid)
            if not content_list:
                raise RuntimeError("MinerU returned empty content_list")

            # 3. Save parquet
            save_parquet(aid, content_list)
            ok += 1
            processed.add(aid)
            log.info("synced", arxiv_id=aid, content_list_items=len(content_list))

        except Exception as exc:
            fail += 1
            log.error("failed to sync", arxiv_id=aid, error=str(exc))
            errs = cp.get("errors", [])
            errs.append({"arxiv_id": aid, "error": str(exc)})
            cp["errors"] = errs

        # Periodic checkpoint
        if (ok + fail) % _CP_INTERVAL == 0 and (ok + fail) > 0:
            cp["processed"] = sorted(processed)
            cp["total_seen"] = ok + skip + fail
            save_checkpoint(cp)
            log.info("checkpoint", ok=ok, skip=skip, fail=fail)

    # Final checkpoint
    cp["processed"] = sorted(processed)
    cp["total_seen"] = ok + skip + fail
    save_checkpoint(cp)
    return {"ok": ok, "skip": skip, "fail": fail}


# ── CLI ─────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="sync_arxiv — 补齐 + 增量同步 arXiv 新论文全文")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--backfill", action="store_true", help="一次性补齐: 2025-09-01 → 今天")
    group.add_argument("--daily", action="store_true", help="每日增量: 昨天的新论文")
    group.add_argument("--range", action="store_true", help="指定日期范围 (需 --from --to)")

    parser.add_argument("--from", dest="from_date", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--resume", help="OAI-PMH resumptionToken 续传")
    parser.add_argument("--dry-run", action="store_true", help="预览模式: 只统计不下载")
    parser.add_argument("--concurrency", type=int, default=4, help="并发论文处理数 (默认 4)")
    args = parser.parse_args()

    if args.backfill:
        from_date = "2025-09-01"
        to_date = time.strftime("%Y-%m-%d")
    elif args.daily:
        yesterday = time.time() - 86400
        from_date = time.strftime("%Y-%m-%d", time.gmtime(yesterday))
        to_date = from_date
    elif args.range:
        if not args.from_date or not args.to_date:
            parser.error("--range requires --from and --to")
        from_date = args.from_date
        to_date = args.to_date
    else:
        parser.error("must specify --backfill, --daily, or --range")

    prefix = "[DRY-RUN] " if args.dry_run else ""
    log.info(f"{prefix}sync start", from_date=from_date, to_date=to_date)

    # 1. Fetch paper list from OAI-PMH
    log.info("fetching papers from OAI-PMH...")
    papers = iter_papers_oai(from_date, to_date, resume_token=args.resume or "")
    log.info("papers fetched", count=len(papers))

    if args.dry_run:
        existing = sum(1 for p in papers if parquet_exists(p["arxiv_id"]))
        log.info(
            "dry-run summary",
            total=len(papers),
            already_parsed=existing,
            would_process=len(papers) - existing,
        )
        return

    # 2. Process
    result = asyncio.run(process_papers(papers, dry_run=False))
    log.info("sync complete", **result)


if __name__ == "__main__":
    main()
