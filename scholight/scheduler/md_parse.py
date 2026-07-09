"""Markdown parse daemon (Stage C).

Converts PDFs and LaTeX sources to markdown for papers that have either
resource but no markdown yet.  Prefers PDF fast-mode (~0.05 s/paper)
over LaTeX/pandoc conversion.

Per-item isolation: a failure on paper X never blocks paper Y.
"""

from __future__ import annotations

from typing import Any

import structlog

from scholight.pipeline.latex_md import latex_to_markdown
from scholight.pipeline.pdf_md import pdf_to_markdown
from scholight.scheduler.base import BaseDaemon, BatchResult
from scholight.storage import storage
from scholight.store.client import get_client
from scholight.store.ingest import update_arxiv_paper

logger = structlog.get_logger(__name__)


class MdParseDaemon(BaseDaemon):
    """Convert PDFs/LaTeX to markdown for papers that need it.

    Each poll queries Milvus for papers where ``has_markdown == false``
    and either ``has_pdf == true`` or ``has_latex == true``, then
    processes them independently.  PDF fast-mode is preferred; LaTeX is
    a fallback.

    Processed papers are tracked in ``done.txt``; permanently failed
    papers (neither resource available) go to ``failed.txt``; transient
    failures are retried on the next poll.
    """

    name = "md_parse"
    sleep_interval = 300
    batch_size = 1000

    # ── Main batch ───────────────────────────────────────────────────

    def process_batch(self) -> BatchResult:
        papers = self._fetch_work()
        result = BatchResult()
        done = self._load_checkpoint()
        failed_set = self._load_failed_checkpoint()
        for paper in papers:
            aid = paper["arxiv_id"]
            if aid in done or aid in failed_set:
                result.skipped += 1
                continue
            try:
                ok = self._parse_one(aid, paper)
                if ok:
                    update_arxiv_paper(aid, {"has_markdown": True})
                    self._save_checkpoint(aid)
                    result.processed += 1
                else:
                    self._failed_checkpoint(aid)
                    result.failed += 1
            except Exception:
                assert self._log is not None
                self._log.exception("parse failed, deferring", arxiv_id=aid)
                result.failed += 1
        return result

    # ── Work fetch ───────────────────────────────────────────────────

    def _fetch_work(self) -> list[dict[str, Any]]:
        client = get_client()
        rows = client.query(
            "arxiv_papers",
            filter="has_markdown == false and (has_pdf == true or has_latex == true)",
            limit=self.batch_size,
            output_fields=["arxiv_id", "created", "has_pdf", "has_latex"],
        )
        return rows if rows else []

    # ── Single paper ─────────────────────────────────────────────────

    def _parse_one(self, aid: str, paper: dict[str, Any]) -> bool:
        """Convert one paper to markdown.  Returns False when neither
        PDF nor LaTeX source is actually available."""
        created = paper["created"]
        if paper.get("has_pdf"):
            md = pdf_to_markdown(storage.pdf_path(aid, created), fast=True)
        elif paper.get("has_latex"):
            md = latex_to_markdown(storage.latex_dir(aid, created))
        else:
            return False
        md_path = storage.markdown_path(aid, created)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md)
        return True
