"""SearchEngine — multi-level search pipeline facade.

Dispatches to level-specific ``Pipeline`` implementations.
"""

from __future__ import annotations

import asyncio
import copy
import math
import time
from collections import Counter
from collections.abc import Sequence
from datetime import date
from typing import Any

import grpc
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

        # ── Sanitize, deterministically order, then truncate ────────
        candidates = _sanitize_and_sort_candidates(ctx.raw_hits)
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
            query_length=len(request.query),
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
    return isinstance(exc, (grpc.RpcError, MilvusException, OSError, TimeoutError)) or is_transient(
        exc
    )


# ── Hit construction ─────────────────────────────────────────────────────────


# NOTE: ``abstract``, ``license``, ``doi``, ``journal_ref``, ``acm_class``,
# ``comments``, ``updated_history`` and similar heavy payload fields are
# EXCLUDED from ``PAPER_SEARCH_FIELDS`` / ``PAPER_SEARCH_WITH_EMBEDDING``
# to reduce Zilliz Read vCU.  They will be empty strings in search results
# until the caller fetches full metadata via a secondary O(1) ``client.query()``
# by arxiv_id.


def _sanitize_and_sort_candidates(raw_hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop unrankable rows and normalize incomplete display metadata."""
    candidates: list[dict[str, Any]] = []
    dropped: Counter[str] = Counter()
    normalized: Counter[str] = Counter()
    for raw in raw_hits:
        raw_score = raw.get("score")
        if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
            dropped["score"] += 1
            continue
        score = float(raw_score)
        if not math.isfinite(score):
            dropped["score"] += 1
            continue

        arxiv_id = raw.get("arxiv_id")
        if not isinstance(arxiv_id, str) or not arxiv_id.strip():
            dropped["arxiv_id"] += 1
            continue
        normalized_arxiv_id = arxiv_id.strip()

        title = raw.get("title")
        normalized_title = title.strip() if isinstance(title, str) else ""
        if not normalized_title:
            normalized["title"] += 1
            normalized_title = f"arXiv:{normalized_arxiv_id}"

        created = _coerce_date(raw.get("created"))
        if created is None and raw.get("created") not in (None, ""):
            normalized["created"] += 1
        updated = _coerce_date(raw.get("updated"))
        if updated is None and raw.get("updated") not in (None, ""):
            normalized["updated"] += 1

        version = _coerce_positive_int(raw.get("version"))
        if version is None and raw.get("version") is not None:
            normalized["version"] += 1

        candidate = dict(raw)
        candidate["score"] = score
        candidate["arxiv_id"] = normalized_arxiv_id
        candidate["title"] = normalized_title
        candidate["created"] = created
        candidate["updated"] = updated
        candidate["version"] = version
        candidates.append(candidate)

    if dropped or normalized:
        logger.warning(
            "search_candidates_sanitized",
            candidate_count=len(raw_hits),
            kept_count=len(candidates),
            dropped=dict(dropped),
            normalized=dict(normalized),
        )

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
                abstract=_coerce_optional_text(raw.get("abstract")),
                categories=_coerce_list(raw.get("categories")),
                created=_coerce_date(raw.get("created")),
                updated=_coerce_date(raw.get("updated")),
                version=_coerce_positive_int(raw.get("version")),
                updated_history=_coerce_list(raw.get("updated_history")),
                license=_coerce_text(raw.get("license")),
                comments=_coerce_text(raw.get("comments")),
                doi=_coerce_text(raw.get("doi")),
                journal_ref=_coerce_text(raw.get("journal_ref")),
                acm_class=_coerce_text(raw.get("acm_class")),
                chunks=chunks,
            )
        )
    return hits


def _coerce_list(value: object) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [str(v) for v in value]
    if value and isinstance(value, str):
        s = value.strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
            return [s[1:-1]]
        return [s] if s else []
    return []


def _coerce_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _coerce_optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _coerce_date(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    try:
        parsed = date.fromisoformat(normalized)
    except ValueError:
        return None
    return normalized if parsed.isoformat() == normalized else None


def _coerce_positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 1 else None
    try:
        parsed = int(str(value))
    except (ValueError, TypeError):
        return None
    return parsed if parsed >= 1 else None


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
