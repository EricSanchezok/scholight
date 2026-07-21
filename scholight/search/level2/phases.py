"""Phase implementations for Level 2 (chunk-level) search.

Two-stage BM25→Dense architecture:
1. BM25 sparse inverted-index lookup (~50 ms) across all 1.72 亿 chunks
   → candidate arxiv_ids (independent of collection size).
2. Dense COSINE ANN restricted to those candidates (~200 ms)
   → precise chunk ranking.
3. MaxP+SumP aggregation: chunk scores → paper-level scores.
4. Weighted RRF fusion: merge independent paper + chunk rankings.

This replaces the old single-stage dense-on-all approach, which scanned
the full collection and suffered from 0.3~27s latency fluctuation
(Zilliz Serverless CU cold-start / HNSW page faults).
"""

from __future__ import annotations

import asyncio
import threading
from collections import defaultdict
from typing import Any

import structlog
from pymilvus.exceptions import MilvusException

from scholight.config import settings
from scholight.search.base import Phase, PipelineContext
from scholight.store.client import get_client
from scholight.store.fields import CHUNK_SEARCH_FIELDS
from scholight.store.query import (
    batch_get_arxiv_papers,
    bm25_search_all_chunks,
    search_arxiv_chunks,
)

logger = structlog.get_logger(__name__)

_CHUNK_LOADED: bool = False
_CHUNK_LOAD_LOCK = threading.Lock()
_CHUNK_OUTPUT_FIELDS: list[str] = list(CHUNK_SEARCH_FIELDS)


def _ensure_chunks_loaded() -> None:
    global _CHUNK_LOADED
    if _CHUNK_LOADED:
        return

    with _CHUNK_LOAD_LOCK:
        if _CHUNK_LOADED:
            return

        client = get_client()
        try:
            if (
                client.get_load_state(
                    "arxiv_chunks", timeout=settings.search_level2_rpc_timeout_seconds
                ).get("state")
                == "LoadStateLoaded"
            ):
                _CHUNK_LOADED = True
                return
        except (MilvusException, OSError, TimeoutError):
            pass

        logger.info("loading arxiv_chunks collection")
        client.load_collection("arxiv_chunks", timeout=settings.search_level2_rpc_timeout_seconds)
        _CHUNK_LOADED = True


class ChunkSearchPhase(Phase):
    """Two-stage BM25→Dense chunk search.

    Stage 1 — BM25 coarse recall: inverted-index lookup (~50 ms) across
    the full 1.72 亿 chunk collection via Zilliz BM25 Function.
    Yields a candidate set of unique arxiv_ids.

    Stage 2 — Dense refine: COSINE ANN restricted to those candidates
    (~200 ms), using the existing ``search_arxiv_chunks`` batched filter
    path to avoid oversized scalar expressions.
    """

    name = "chunk_search"

    async def execute(self, ctx: PipelineContext) -> None:
        if ctx.query_vector is None:
            raise ValueError("query_vector not set — EmbedPhase must run first")
        if not ctx.request.query:
            raise ValueError("query text required for BM25 coarse search")

        await asyncio.to_thread(_ensure_chunks_loaded)
        query_text = ctx.request.query

        # ── Stage 1: BM25 coarse recall ──────────────────────────
        bm25_hits = await asyncio.to_thread(
            bm25_search_all_chunks,
            query_text=query_text,
            top_k=settings.bm25_coarse_top_k,
            output_fields=["chunk_id", "arxiv_id"],
            timeout=settings.search_level2_rpc_timeout_seconds,
        )
        # Deduplicate to unique paper IDs
        candidate_ids = list(dict.fromkeys(h["arxiv_id"] for h in bm25_hits))
        ctx.metadata["bm25_chunk_candidates"] = len(bm25_hits)
        ctx.metadata["bm25_paper_candidates"] = len(candidate_ids)
        logger.debug(
            "bm25 coarse complete",
            chunks=len(bm25_hits),
            papers=len(candidate_ids),
        )

        request = ctx.request
        if any((request.categories, request.authors, request.date_from, request.date_to)):
            eligible_papers = await asyncio.to_thread(
                batch_get_arxiv_papers,
                candidate_ids,
                categories=request.categories,
                authors=request.authors,
                date_from=request.date_from,
                date_to=request.date_to,
                output_fields=["arxiv_id"],
                timeout=settings.search_level2_rpc_timeout_seconds,
            )
            candidate_ids = [aid for aid in candidate_ids if aid in eligible_papers]
        ctx.metadata["filtered_chunk_paper_candidates"] = len(candidate_ids)

        # ── Stage 2: Dense refine on candidates ──────────────────
        if not candidate_ids:
            ctx.chunk_hits = []
            ctx.metadata["chunk_candidates"] = 0
            return

        chunks = await asyncio.to_thread(
            search_arxiv_chunks,
            query_vector=ctx.query_vector,
            arxiv_ids=candidate_ids,
            top_k=settings.dense_refine_top_k,
            output_fields=_CHUNK_OUTPUT_FIELDS,
            timeout=settings.search_level2_rpc_timeout_seconds,
        )

        ctx.chunk_hits = chunks
        ctx.metadata["chunk_candidates"] = len(chunks)
        ctx.metadata["chunk_mode"] = "bm25+dense"
        ctx.metadata["chunk_level"] = settings.chunk_search_level
        logger.debug(
            "chunk search complete",
            chunks=len(chunks),
            papers=len(candidate_ids),
        )


class MaxPAggregationPhase(Phase):
    """C1 MaxP+SumP blend + C3 position weighting.

    C1 (Nardini et al. SIGIR 2025):
      paper_score = alpha * max(top-5) + (1-alpha) * mean(top-5)
    C3 (CoRank Tian 2025 / OMRC-MR Wang 2025):
      later chunks (results/conclusion) get a position boost.

    NOTE: ``chunk_idx``, ``heading``, and ``content_text`` are NOT in
    ``CHUNK_SEARCH_FIELDS`` (reduced Zilliz Read vCU payload).  Position
    weighting degrades gracefully (all chunk_idx default to 0) and evidence
    only carries chunk_id/chunk_idx/score — no snippet or heading.
    Callers needing full content should fetch chunks via a secondary
    ``client.query()`` by chunk_id.
    """

    name = "chunk_aggregation"

    async def execute(self, ctx: PipelineContext) -> None:
        chunks = ctx.chunk_hits
        if not chunks:
            ctx.metadata["chunk_paper_scores"] = {}
            ctx.metadata["chunk_evidence"] = {}
            ctx.metadata["chunk_paper_count"] = 0
            return

        alpha = settings.search_chunk_aggregation_alpha
        beta = settings.search_position_weight_beta

        # C3: position-weight individual chunk scores.
        # chunk_idx defaults to 0 when not in output_fields, making
        # position weighting effectively disabled with current schema.
        max_idx: dict[str, int] = {}
        for ch in chunks:
            aid = ch["arxiv_id"]
            idx = ch.get("chunk_idx", 0)
            max_idx[aid] = max(max_idx.get(aid, 0), idx + 1)
        for ch in chunks:
            aid = ch["arxiv_id"]
            if beta > 0 and (n := max_idx.get(aid, 1)) > 1:
                pos = ch.get("chunk_idx", 0) / max(n - 1, 1)
                ch["score"] *= 1.0 + beta * pos

        # Group by paper
        groups: dict[str, list[float]] = defaultdict(list)
        chunk_pool: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for ch in chunks:
            aid = ch["arxiv_id"]
            groups[aid].append(ch.get("score", 0.0))
            chunk_pool[aid].append(ch)

        # C1: MaxP+SumP blend + evidence
        chunk_paper_scores: dict[str, float] = {}
        chunk_evidence: dict[str, list[dict[str, Any]]] = {}
        for arxiv_id, scores in groups.items():
            scores.sort(reverse=True)
            top5 = scores[:5]
            maxp = max(top5)
            sump = sum(top5) / max(len(top5), 1)
            chunk_paper_scores[arxiv_id] = alpha * maxp + (1.0 - alpha) * sump

            paper_chunks = sorted(
                chunk_pool[arxiv_id], key=lambda c: c.get("score", 0.0), reverse=True
            )
            chunk_evidence[arxiv_id] = [
                {
                    "chunk_id": c["chunk_id"],
                    "chunk_idx": c.get("chunk_idx"),
                    "score": c.get("score", 0.0),
                }
                for c in paper_chunks[:3]
            ]

        ctx.metadata["chunk_paper_scores"] = chunk_paper_scores
        ctx.metadata["chunk_evidence"] = chunk_evidence
        ctx.metadata["chunk_paper_count"] = len(chunk_paper_scores)
        logger.debug("chunk aggregation complete", papers=len(chunk_paper_scores))


class RRFFusionPhase(Phase):
    """C2 weighted RRF: rrf = w_paper/(k+pr) + w_chunk/(k+cr).

    Every paper — whether discovered by L1, the chunk pathway, or both —
    receives the same RRF-derived ``score``.  The original L1 COSINE
    distance is preserved as ``paper_score`` for diagnostics but does NOT
    leak into the public ``score`` field.

    Chunk-only papers also have metadata (title, authors, categories,
    etc.) back-filled from arxiv_papers.
    """

    name = "rrf_fusion"

    async def execute(self, ctx: PipelineContext) -> None:
        k = settings.search_rrf_k
        w_paper = settings.search_rrf_paper_weight
        w_chunk = settings.search_rrf_chunk_weight

        paper_rank = {h["arxiv_id"]: i for i, h in enumerate(ctx.raw_hits)}
        chunk_scores = ctx.metadata.get("chunk_paper_scores", {})
        sorted_c = sorted(chunk_scores.items(), key=lambda x: x[1], reverse=True)
        chunk_rank = {aid: i for i, (aid, _) in enumerate(sorted_c)}

        rrf: dict[str, float] = {}
        for aid in set(paper_rank) | set(chunk_rank):
            s = 0.0
            if aid in paper_rank:
                s += w_paper / (k + paper_rank[aid] + 1)
            if aid in chunk_rank:
                s += w_chunk / (k + chunk_rank[aid] + 1)
            rrf[aid] = s

        existing = {h["arxiv_id"]: h for h in ctx.raw_hits}
        merged: list[dict[str, Any]] = []

        for hit in ctx.raw_hits:
            aid = hit["arxiv_id"]
            if aid in rrf:
                original = hit.get("score", 0.0)
                hit = dict(hit)
                hit["paper_score"] = original
                hit["rrf_score"] = rrf[aid]
                # Unified RRF-derived score for all L2 papers
                hit["score"] = rrf[aid]
            merged.append(hit)

        # Collect arxiv_ids that need metadata backfill
        missing_ids = [aid for aid in chunk_rank if aid not in existing]
        paper_map = (
            await asyncio.to_thread(
                batch_get_arxiv_papers,
                missing_ids,
                timeout=settings.search_level2_rpc_timeout_seconds,
            )
            if missing_ids
            else {}
        )

        for aid in chunk_rank:
            if aid in existing:
                continue
            paper = paper_map.get(aid)
            if paper is None:
                logger.warning("level2_metadata_backfill_missing", arxiv_id=aid)
                continue
            merged.append(
                {
                    "arxiv_id": aid,
                    "score": rrf.get(aid, 0.0),
                    "rrf_score": rrf.get(aid, 0.0),
                    "paper_score": 0.0,
                    "title": paper.get("title", ""),
                    "abstract": paper.get("abstract", ""),
                    "authors": paper.get("authors", []),
                    "categories": paper.get("categories", []),
                    "created": paper.get("created", ""),
                    "updated": paper.get("updated", ""),
                    "version": paper.get("version", 0),
                    "license": paper.get("license", ""),
                    "comments": paper.get("comments", ""),
                    "doi": paper.get("doi", ""),
                    "journal_ref": paper.get("journal_ref", ""),
                    "acm_class": paper.get("acm_class", ""),
                    "chunk_only": True,
                }
            )

        merged.sort(key=lambda h: h.get("rrf_score", 0.0), reverse=True)
        ctx.raw_hits = merged
        ctx.metadata["rrf_paper_count"] = len(merged)
        c_only = sum(1 for h in merged if h.get("chunk_only"))
        ctx.metadata["rrf_chunk_only_papers"] = c_only
        logger.debug("rrf fusion complete", papers=len(merged), chunk_only=c_only)
