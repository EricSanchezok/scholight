"""chunk_ingest.py — Stage D daemon: chunk → embed → insert.

Polls arxiv_papers for records where has_markdown=True and has_chunks=False,
then runs the full chunk ingestion pipeline per paper with per-item isolation.

Pipeline:
  1. Read paper.md → detect source (latex/pdf)
  2. chunk_markdown → list of chunk dicts
  3. Dense embedding via Embedder (batch per paper)
  4. upsert_arxiv_chunks → arxiv_chunks collection
  5. update_arxiv_paper(has_chunks=True)
  6. Checkpoint to done.txt

BM25 sparse vectors are handled by a Zilliz Cloud Function on ``content_bm25``,
so no Python-side sparse encoding is needed.

Deployment: ``scholight scheduler chunk-daemon`` (or cron/systemd timer).
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Any

from scholight.pipeline.chunkers.md_chunker import chunk_markdown
from scholight.pipeline.embedder import Embedder
from scholight.scheduler.base import BaseDaemon, BatchResult
from scholight.storage import storage
from scholight.store.client import get_client
from scholight.store.ingest import update_arxiv_paper, upsert_arxiv_chunks

# ── Per-chunk field limits (matches schema) ────────────────────────────

MD_MAX_LEN = 16384  # content_text VARCHAR(16384)


class ChunkIngestDaemon(BaseDaemon):
    """Daemon that processes papers needing chunk ingestion.

    Attributes:
        name: ``"chunk_ingest"`` — checkpoint + log subdirectory.
        sleep_interval: Seconds between polls when idle (fully caught up).
        batch_size: Maximum papers per ``process_batch()`` call.
            Kept small because each paper triggers an embedding API call.
    """

    name = "chunk_ingest"
    sleep_interval = 300
    batch_size = 50

    def on_startup(self) -> None:
        """No-op: BM25 sparse vectors are handled by a Zilliz Cloud Function.

        Override base ``on_startup`` to prevent ``NotImplementedError``.
        """
        return

    # ── Main poll ─────────────────────────────────────────────────────

    def process_batch(self) -> BatchResult:
        """Fetch, chunk, embed, and insert one batch of papers.

        Each paper is processed independently — a failure on paper X never
        blocks paper Y.  Dense embedding uses the batch per-paper via
        ``asyncio.run``; BM25 sparse vectors are handled by Zilliz Cloud.
        """
        papers = self._fetch_work()
        result = BatchResult()
        done = self._load_checkpoint()
        failed_set = self._load_failed_checkpoint()

        for paper in papers:
            aid = str(paper["arxiv_id"])
            if aid in done or aid in failed_set:
                result.skipped += 1
                continue
            try:
                ok = asyncio.run(self._process_one(aid, paper))
                if ok:
                    update_arxiv_paper(aid, {"has_chunks": True})
                    self._save_checkpoint(aid)
                    result.processed += 1
                else:
                    self._failed_checkpoint(aid)
                    result.failed += 1
            except Exception:
                assert self._log is not None
                self._log.exception("ingest failed, deferring", arxiv_id=aid)
                result.failed += 1

        return result

    # ── Per-paper pipeline ────────────────────────────────────────────

    async def _process_one(self, aid: str, paper: dict[str, Any]) -> bool:
        """Run the full chunk pipeline for a single paper.

        Returns ``True`` if chunks were produced and inserted successfully,
        ``False`` if no chunks could be produced (missing markdown, empty
        text, chunker produced nothing).
        """
        created = str(paper.get("created", ""))

        # ── Read markdown ─────────────────────────────────────────
        md_path = storage.markdown_path(aid, created)
        if not md_path.exists():
            return False
        md_text = md_path.read_text(encoding="utf-8")

        # ── Detect source ─────────────────────────────────────────
        source = "latex" if md_text.startswith("---\n") else "pdf"

        # ── Chunk ─────────────────────────────────────────────────
        md_chunks = chunk_markdown(md_text, source=source)
        if not md_chunks:
            return False

        chunk_dicts: list[dict[str, Any]] = []
        for mc in md_chunks:
            content = mc.content[:MD_MAX_LEN]
            if not content.strip():
                continue
            chunk_dicts.append(
                {
                    "chunk_id": f"{aid}::chunk::{mc.chunk_index}",
                    "arxiv_id": aid,
                    "chunk_idx": mc.chunk_index,
                    "content_text": content,
                }
            )

        if not chunk_dicts:
            return False

        # ── Dense embedding ───────────────────────────────────────
        texts = [str(ch_dict["content_text"]) for ch_dict in chunk_dicts]
        async with Embedder() as emb:
            embeddings = await emb.embed_many(texts)
        if len(embeddings) != len(texts):
            return False
        for ch_dict, vec in zip(chunk_dicts, embeddings):
            ch_dict["content_embedding"] = vec

        # ── BM25: handled by Zilliz Cloud Function (auto-populates content_bm25)
        # No Python-side sparse encoding needed.

        # ── Insert ────────────────────────────────────────────────
        upsert_arxiv_chunks(chunk_dicts)

        # ── 清理本地中间产物（chunk 已入库 Zilliz，不再需要） ──
        paper_dir = storage._paper_dir(aid, created)  # noqa: SLF001
        if paper_dir.exists():
            shutil.rmtree(paper_dir)

        return True

    # ── Work fetch ────────────────────────────────────────────────────

    @staticmethod
    def _fetch_work() -> list[dict[str, Any]]:
        """Query papers where has_markdown=True and has_chunks=False."""
        from typing import cast

        return cast(
            "list[dict[str, Any]]",
            get_client().query(
                "arxiv_papers",
                filter="has_markdown == true and has_chunks == false",
                output_fields=["arxiv_id", "title", "created"],
                limit=ChunkIngestDaemon.batch_size,
            ),
        )
