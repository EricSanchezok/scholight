"""SearchEngine — multi-level search pipeline facade.

Dispatches to level-specific ``Pipeline`` implementations.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from compass.config import settings
from compass.models.search import (
    PhaseTiming,
    SearchHit,
    SearchRequest,
    SearchResult,
    SearchStats,
)
from compass.search.level1 import LEVEL1_STRATEGIES, Level1Pipeline
from compass.search.level2 import LEVEL2_STRATEGIES, Level2Pipeline
from compass.store.client import get_client

logger = structlog.get_logger(__name__)


class SearchEngine:
    """Multi-level academic paper search pipeline (thin facade)."""

    async def search(self, request: SearchRequest) -> SearchResult:
        if request.level not in (1, 2):
            raise NotImplementedError(f"Search level {request.level} is not yet implemented")

        t_start = time.perf_counter()

        # ── Level 1: paper search always runs first ──────────────────
        l1_pipeline = self._resolve_l1_pipeline(request)
        l1_ctx = await l1_pipeline.run(request)

        level1_phases = [p.name for p in l1_pipeline.phases]

        # ── Level 2: independent chunk pathway + RRF fusion ──────────
        chunk_evidence = None
        if request.level >= 2:
            l2_pipeline = self._resolve_l2_pipeline(request)
            await l2_pipeline.run(request, ctx=l1_ctx)
            chunk_evidence = l1_ctx.metadata.get("chunk_evidence")
            logger.info(
                "l2 fusion complete",
                rrf_papers=l1_ctx.metadata.get("rrf_paper_count", 0),
                chunk_only=l1_ctx.metadata.get("rrf_chunk_only_papers", 0),
                evidence_count=len(chunk_evidence) if chunk_evidence else 0,
            )

        # ── Recover metadata for stats ──────────────────────────────
        ctx = l1_ctx
        embed_ms = ctx.timings.get("embed_query", 0.0)
        search_ms = ctx.timings.get("paper_search", 0.0)
        fuse_ms = ctx.timings.get("score_fusion", 0.0)
        chunk_search_ms = ctx.timings.get("chunk_search", 0.0)
        chunk_agg_ms = ctx.timings.get("chunk_aggregation", 0.0)
        rrf_ms = ctx.timings.get("rrf_fusion", 0.0)
        use_hybrid = ctx.metadata.get("mode") == "hybrid"
        n_candidates = len(ctx.raw_hits)
        n_chunks = ctx.metadata.get("chunk_candidates", 0)
        n_chunk_papers = ctx.metadata.get("chunk_paper_count", 0)

        # ── Assemble result ──────────────────────────────────────────
        hits = _build_hits(ctx.raw_hits, request.top_k, chunk_evidence)

        phases = [
            PhaseTiming(phase="embed_query", duration_ms=round(embed_ms, 2)),
            PhaseTiming(
                phase="paper_search",
                duration_ms=round(search_ms, 2),
                metadata={
                    "mode": "hybrid" if use_hybrid else "dense",
                },
            ),
        ]
        if "score_fusion" in level1_phases:
            phases.append(
                PhaseTiming(
                    phase="score_fusion",
                    duration_ms=round(fuse_ms, 2),
                    metadata={"candidates": n_candidates, "enabled": request.enable_fusion},
                )
            )
        if request.level >= 2:
            phases.append(
                PhaseTiming(
                    phase="chunk_search",
                    duration_ms=round(chunk_search_ms, 2),
                    metadata={
                        "candidates": n_chunks,
                        "mode": ctx.metadata.get("chunk_mode", "dense"),
                    },
                )
            )
            phases.append(
                PhaseTiming(
                    phase="chunk_aggregation",
                    duration_ms=round(chunk_agg_ms, 2),
                    metadata={"paper_count": n_chunk_papers, "method": "MaxP"},
                )
            )
            phases.append(
                PhaseTiming(
                    phase="rrf_fusion",
                    duration_ms=round(rrf_ms, 2),
                    metadata={"method": "RRF(k=60)"},
                )
            )

        stats = SearchStats(
            level=request.level,
            embedding_model=settings.embedding_model,
            embedding_dim=settings.embedding_dim,
            paper_candidates=n_candidates,
            phases=phases,
        )

        total_papers, total_chunks = _collection_row_counts()
        total_ms = (time.perf_counter() - t_start) * 1000

        logger.info(
            "search completed",
            query=request.query[:80],
            level=request.level,
            mode="hybrid" if use_hybrid else "dense",
            hits=len(hits),
            candidates=n_candidates,
            total_ms=round(total_ms, 2),
        )

        return SearchResult(
            query=request.query,
            level=request.level,
            total_ms=round(total_ms, 2),
            hits=hits,
            stats=stats,
            total_papers=total_papers,
            total_chunks=total_chunks,
        )

    # ── Pipeline resolution ───────────────────────────────────────

    @staticmethod
    def _resolve_l1_pipeline(request: SearchRequest) -> Level1Pipeline:
        """Resolve Level 1 pipeline from strategy or individual toggles."""
        if request.strategy and request.strategy in LEVEL1_STRATEGIES:
            return Level1Pipeline(phases=list(LEVEL1_STRATEGIES[request.strategy]))
        return Level1Pipeline()

    @staticmethod
    def _resolve_l2_pipeline(_request: SearchRequest) -> Level2Pipeline:
        """Resolve Level 2 pipeline using RRF strategy (default)."""
        return Level2Pipeline(extra_phases=list(LEVEL2_STRATEGIES["rrf"]))


# ── Hit construction ─────────────────────────────────────────────────────────


# NOTE: ``abstract``, ``license``, ``doi``, ``journal_ref``, ``acm_class``,
# ``comments``, ``updated_history`` and similar heavy payload fields are
# EXCLUDED from ``PAPER_SEARCH_FIELDS`` / ``PAPER_SEARCH_WITH_EMBEDDING``
# to reduce Zilliz Read vCU.  They will be empty strings in search results
# until the caller fetches full metadata via a secondary O(1) ``client.query()``
# by arxiv_id.


def _build_hits(
    raw_hits: list[dict[str, Any]],
    top_k: int,
    chunk_evidence: dict[str, list[dict[str, Any]]] | None = None,
) -> list[SearchHit]:
    hits: list[SearchHit] = []
    evidence = chunk_evidence or {}
    for rank, raw in enumerate(raw_hits[:top_k], start=1):
        aid = raw.get("arxiv_id", "")
        chunks = [
            {
                "chunk_id": c.get("chunk_id", ""),
                "chunk_idx": c.get("chunk_idx"),
                "score": c.get("score", 0.0),
            }
            for c in evidence.get(aid, [])
        ]
        hits.append(
            SearchHit(
                rank=rank,
                score=raw.get("score", 0.0),
                arxiv_id=aid,
                title=raw.get("title", ""),
                authors=_coerce_list(raw.get("authors")),
                abstract=raw.get("abstract", ""),
                categories=_coerce_list(raw.get("categories")),
                created=raw.get("created", ""),
                updated=raw.get("updated", ""),
                version=_coerce_int(raw.get("version")),
                updated_history=_coerce_list(raw.get("updated_history")),
                license=raw.get("license", ""),
                comments=raw.get("comments", ""),
                doi=raw.get("doi", ""),
                journal_ref=raw.get("journal_ref", ""),
                acm_class=raw.get("acm_class", ""),
                chunks=chunks,
            )
        )
    return hits


def _coerce_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if value and isinstance(value, str):
        s = value.strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
            return [s[1:-1]]
        return [s] if s else []
    return []


def _coerce_int(value: object) -> int:
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (ValueError, TypeError):
        return 0


def _collection_row_counts() -> tuple[int | None, int | None]:
    try:
        client = get_client()
    except Exception:
        logger.debug("Zilliz Cloud client unavailable for collection stats")
        return None, None

    total_papers: int | None = None
    total_chunks: int | None = None

    for coll_name in ("arxiv_papers", "arxiv_chunks"):
        try:
            coll_stats = client.get_collection_stats(coll_name)
            row_count: int = coll_stats.get("row_count", 0)
        except Exception:
            logger.debug("failed to get collection stats", collection=coll_name)
            row_count = 0

        if coll_name == "arxiv_papers":
            total_papers = row_count
        elif coll_name == "arxiv_chunks":
            total_chunks = row_count

    return total_papers, total_chunks


__all__ = ["SearchEngine"]
