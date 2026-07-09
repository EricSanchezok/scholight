"""Level 1 search phases — composed from SearchEngine logic."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from compass.config import settings
from compass.pipeline.embedder import Embedder
from compass.search.base import Phase, PipelineContext
from compass.search.common.fusion import rerank_candidates as fuse_scores
from compass.store.fields import PAPER_SEARCH_WITH_EMBEDDING
from compass.store.query import hybrid_search_arxiv_papers, search_arxiv_papers

logger = structlog.get_logger(__name__)

_PAPER_OUTPUT_FIELDS: list[str] = list(PAPER_SEARCH_WITH_EMBEDDING)


def _do_paper_search(
    query_vector: list[float],
    query_text: str,
    top_k: int,
    *,
    use_hybrid: bool,
    categories: list[str] | None,
    authors: list[str] | None,
    date_from: str | None,
    date_to: str | None,
    arxiv_ids: list[str] | None,
) -> list[dict[str, Any]]:
    """Dispatch between dense and hybrid paper search."""
    if not use_hybrid:
        return search_arxiv_papers(
            query_vector=query_vector,
            top_k=top_k,
            categories=categories,
            authors=authors,
            date_from=date_from,
            date_to=date_to,
            arxiv_ids=arxiv_ids,
            output_fields=_PAPER_OUTPUT_FIELDS,
        )
    return hybrid_search_arxiv_papers(
        query_vector=query_vector,
        query_text=query_text,
        top_k=top_k,
        categories=categories,
        authors=authors,
        date_from=date_from,
        date_to=date_to,
        output_fields=_PAPER_OUTPUT_FIELDS,
    )


class EmbedPhase(Phase):
    """Embed the query text or validate a pre-computed vector."""

    name = "embed_query"

    async def __call__(self, ctx: PipelineContext) -> None:
        if ctx.request.query_vector is not None:
            await self.execute(ctx)
            ctx.record_timing(self.name, 0.0)
        else:
            await super().__call__(ctx)

    async def execute(self, ctx: PipelineContext) -> None:
        if ctx.request.query_vector is not None:
            if len(ctx.request.query_vector) != settings.embedding_dim:
                raise ValueError(
                    f"query_vector dimension {len(ctx.request.query_vector)} "
                    f"does not match configured {settings.embedding_dim}"
                )
            ctx.query_vector = ctx.request.query_vector
        else:
            async with Embedder() as embedder:
                ctx.query_vector = await embedder.embed_single(ctx.request.query)


class AnnSearchPhase(Phase):
    """First-pass paper search: dense or hybrid ANN retrieval.

    BM25 is handled by Zilliz built-in Function — no external encoder needed.
    Hybrid search passes ``request.query`` directly as the BM25 query text.
    """

    name = "paper_search"

    async def execute(self, ctx: PipelineContext) -> None:
        request = ctx.request
        top_k_stage = request.top_k * 10

        use_hybrid = bool(request.sparse_vector or not request.arxiv_ids)

        if ctx.query_vector is None:
            raise ValueError("query_vector not set — EmbedPhase must run first")

        ctx.raw_hits = await asyncio.to_thread(
            _do_paper_search,
            query_vector=ctx.query_vector,
            query_text=request.query,
            top_k=top_k_stage,
            use_hybrid=use_hybrid,
            categories=request.categories,
            authors=request.authors,
            date_from=request.date_from,
            date_to=request.date_to,
            arxiv_ids=request.arxiv_ids,
        )

        ctx.metadata["mode"] = "hybrid" if use_hybrid else "dense"
        ctx.metadata["use_hybrid"] = use_hybrid


class FusionPhase(Phase):
    """Multi-signal score fusion re-ranking."""

    name = "score_fusion"

    async def execute(self, ctx: PipelineContext) -> None:
        request = ctx.request
        n_candidates = len(ctx.raw_hits)

        if not request.enable_fusion:
            return

        try:
            ctx.raw_hits = fuse_scores(
                query=request.query,
                candidates=ctx.raw_hits,
                query_vector=ctx.query_vector,
            )
        except Exception:
            logger.warning("score fusion failed — keeping ANN order", candidates=n_candidates)
