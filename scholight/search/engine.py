"""SearchEngine — multi-level search pipeline facade.

Dispatches to level-specific ``Pipeline`` implementations.
"""

from __future__ import annotations

import asyncio
import copy
import math
import time
from datetime import date
from typing import Any

import structlog
from pymilvus.exceptions import MilvusException

from scholight.config import settings
from scholight.models.search import (
    PhaseTiming,
    SearchHit,
    SearchRequest,
    SearchResult,
    SearchStats,
)
from scholight.search.base import PhaseError
from scholight.search.errors import (
    SearchInvariantError,
    SearchUnavailable,
    ThoroughSearchUnavailable,
)
from scholight.search.level1 import LEVEL1_STRATEGIES, Level1Pipeline
from scholight.search.level2 import LEVEL2_STRATEGIES, Level2Pipeline
from scholight.store.client import get_client
from scholight.utils.http import is_transient

logger = structlog.get_logger(__name__)

_COLLECTION_STATS_TIMEOUT_SECONDS = 0.1
_COLLECTION_STATS_CACHE_SECONDS = 60.0
_collection_stats_cache: tuple[float, tuple[int | None, int | None]] | None = None
_collection_stats_lock: asyncio.Lock | None = None


class SearchEngine:
    """Multi-level academic paper search pipeline (thin facade)."""

    async def search(self, request: SearchRequest) -> SearchResult:
        if request.level not in (1, 2):
            raise NotImplementedError(f"Search level {request.level} is not yet implemented")

        t_start = time.perf_counter()

        # ── Level 1: paper search always runs first ──────────────────
        l1_pipeline = self._resolve_l1_pipeline(request)
        try:
            l1_ctx = await l1_pipeline.run(request)
        except PhaseError as exc:
            if not _is_operational_search_error(exc.cause):
                raise
            raise SearchUnavailable(phase_name=exc.phase_name, cause=exc.cause) from exc

        level1_phases = [p.name for p in l1_pipeline.phases]

        # ── Level 2: strict chunk pathway + RRF fusion ──────────────
        chunk_evidence = None
        if request.level >= 2:
            l2_pipeline = self._resolve_l2_pipeline(request)
            l2_ctx = copy.deepcopy(l1_ctx)
            try:
                await asyncio.wait_for(
                    l2_pipeline.run(request, ctx=l2_ctx),
                    timeout=settings.search_level2_timeout_seconds,
                )
            except TimeoutError as exc:
                raise ThoroughSearchUnavailable(phase_name="level2", cause=exc) from exc
            except PhaseError as exc:
                if not _is_operational_search_error(exc.cause):
                    raise
                raise ThoroughSearchUnavailable(
                    phase_name=exc.phase_name,
                    cause=exc.cause,
                ) from exc

            l1_ctx = l2_ctx
            chunk_evidence = l1_ctx.metadata.get("chunk_evidence")
            logger.info(
                "l2_fusion_complete",
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

        # ── Validate, deterministically order, then truncate ────────
        candidates = _validate_and_sort_candidates(ctx.raw_hits)
        hits = _build_hits(candidates, request.top_k, chunk_evidence)

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

        total_papers, total_chunks = await _collection_row_counts()
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


def _is_operational_search_error(exc: Exception) -> bool:
    """Return whether a required search dependency failed operationally."""
    return isinstance(exc, (MilvusException, OSError, TimeoutError)) or is_transient(exc)


# ── Hit construction ─────────────────────────────────────────────────────────


# NOTE: ``abstract``, ``license``, ``doi``, ``journal_ref``, ``acm_class``,
# ``comments``, ``updated_history`` and similar heavy payload fields are
# EXCLUDED from ``PAPER_SEARCH_FIELDS`` / ``PAPER_SEARCH_WITH_EMBEDDING``
# to reduce Zilliz Read vCU.  They will be empty strings in search results
# until the caller fetches full metadata via a secondary O(1) ``client.query()``
# by arxiv_id.


def _validate_and_sort_candidates(raw_hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate final core fields and return the full deterministic ordering."""
    candidates: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_hits):
        raw_score = raw.get("score")
        if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
            raise SearchInvariantError(f"candidate {index} must have a finite score")
        score = float(raw_score)
        if not math.isfinite(score):
            raise SearchInvariantError(f"candidate {index} must have a finite score")

        arxiv_id = raw.get("arxiv_id")
        if not isinstance(arxiv_id, str) or not arxiv_id.strip():
            raise SearchInvariantError(f"candidate {index} has invalid arxiv_id")
        title = raw.get("title")
        if not isinstance(title, str) or not title.strip():
            raise SearchInvariantError(f"candidate {index} has invalid title")

        for field_name in ("created", "updated"):
            raw_date = raw.get(field_name)
            if not isinstance(raw_date, str) or len(raw_date) != 10:
                raise SearchInvariantError(f"candidate {index} has invalid {field_name}")
            try:
                parsed_date = date.fromisoformat(raw_date)
            except ValueError as exc:
                raise SearchInvariantError(f"candidate {index} has invalid {field_name}") from exc
            if parsed_date.isoformat() != raw_date:
                raise SearchInvariantError(f"candidate {index} has invalid {field_name}")

        version = raw.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise SearchInvariantError(f"candidate {index} has invalid version")

        candidate = dict(raw)
        candidate["score"] = score
        candidates.append(candidate)

    return sorted(candidates, key=lambda candidate: (-candidate["score"], candidate["arxiv_id"]))


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


async def _collection_row_counts() -> tuple[int | None, int | None]:
    global _collection_stats_cache, _collection_stats_lock

    now = time.monotonic()
    if (
        _collection_stats_cache is not None
        and now - _collection_stats_cache[0] < _COLLECTION_STATS_CACHE_SECONDS
    ):
        return _collection_stats_cache[1]

    if _collection_stats_lock is None:
        _collection_stats_lock = asyncio.Lock()
    async with _collection_stats_lock:
        now = time.monotonic()
        if (
            _collection_stats_cache is not None
            and now - _collection_stats_cache[0] < _COLLECTION_STATS_CACHE_SECONDS
        ):
            return _collection_stats_cache[1]

        try:
            row_counts = await asyncio.wait_for(
                asyncio.to_thread(_fetch_collection_row_counts),
                timeout=_COLLECTION_STATS_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.debug("collection stats timed out")
            row_counts = (None, None)

        _collection_stats_cache = (time.monotonic(), row_counts)
        return row_counts


def _fetch_collection_row_counts() -> tuple[int | None, int | None]:
    try:
        client = get_client()
    except Exception:
        logger.debug("Zilliz Cloud client unavailable for collection stats")
        return None, None

    row_counts: list[int | None] = []
    for collection_name in ("arxiv_papers", "arxiv_chunks"):
        try:
            collection_stats = client.get_collection_stats(
                collection_name,
                timeout=_COLLECTION_STATS_TIMEOUT_SECONDS,
            )
            row_counts.append(int(collection_stats.get("row_count", 0)))
        except Exception:
            logger.debug("failed to get collection stats", collection=collection_name)
            row_counts.append(None)

    return row_counts[0], row_counts[1]


__all__ = ["SearchEngine"]
