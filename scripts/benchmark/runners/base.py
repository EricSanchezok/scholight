"""Base runner — shared search + versioning + save logic."""

from __future__ import annotations

import asyncio
import datetime
import json
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class Query:
    id: str
    text: str
    gt_arxiv_ids: list[str]  # ground truth


@dataclass
class QueryResult:
    query_id: str
    question: str
    gt_arxiv_ids: list[str]
    predicted_arxiv_ids: list[str]
    search_ms: float
    pool_arxiv_ids: list[str] = field(default_factory=list)
    hit_ids: list[str] = field(default_factory=list)
    missed_ids: list[str] = field(default_factory=list)

    @property
    def iou(self) -> float:
        gt = set(self.gt_arxiv_ids)
        pred = set(self.predicted_arxiv_ids)
        intersection = len(gt & pred)
        union = len(gt | pred)
        return intersection / union if union else 0.0

    @property
    def recall(self) -> float:
        gt = set(self.gt_arxiv_ids)
        return len(gt & set(self.predicted_arxiv_ids)) / len(gt) if gt else 0.0

    @property
    def precision(self) -> float:
        pred = set(self.predicted_arxiv_ids)
        return len(set(self.gt_arxiv_ids) & pred) / len(pred) if pred else 0.0


class BaseRunner(ABC):
    """Abstract benchmark runner: load → search → eval → save."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)
        self._git_commit: str | None = None

    # ── public API ──────────────────────────────────────────────────────

    def run(
        self,
        top_k: int,
        version: str,
        max_queries: int | None = None,
        level: int = 1,
        concurrency: int = 32,
    ) -> dict[str, Any]:
        """End-to-end: load queries, run search, evaluate, save to versioned dir.

        Args:
            top_k: results per query passed to SearchEngine
            version: version label (e.g. ``"v1.0"``)
            max_queries: if set, only run the first *N* queries (smoke test)
            level: search level (1 = paper-only, 2 = paper+chunk, 3 = full agent)
            concurrency: max concurrent search requests
        """
        queries = self._load_queries()
        if max_queries is not None and max_queries > 0:
            queries = queries[:max_queries]
        logger.info("loaded %d queries (concurrency=%d)", len(queries), concurrency)

        results = asyncio.run(self._batch_search(queries, top_k, level, concurrency))
        agg = self._aggregate(results)

        output_dir = self._version_dir(version, level)
        self._save(
            output_dir,
            agg,
            results,
            {
                "top_k": top_k,
                "level": level,
                "concurrency": concurrency,
                "query_count": len(results),
            },
        )
        logger.info("saved to %s", output_dir)
        return agg

    def latest_version(self, level: int = 1) -> str:
        """Return the latest version string for *level*, or ``'v0.0'`` if none."""
        latest = self._latest_version_dir(level)
        return latest.name if latest else "v0.0"

    # ── subclasses implement these ──────────────────────────────────────

    @abstractmethod
    def _load_queries(self) -> list[Query]: ...

    @abstractmethod
    def _aggregate(self, results: list[QueryResult]) -> dict[str, Any]:
        """Compute benchmark-specific aggregate metrics."""

    # ── shared search ───────────────────────────────────────────────────

    async def _batch_search(
        self, queries: list[Query], top_k: int, level: int = 1, concurrency: int = 32
    ) -> list[QueryResult]:
        from compass.models.search import SearchRequest
        from compass.pipeline.embedder import Embedder
        from compass.search.engine import SearchEngine

        engine = SearchEngine()
        texts = [q.text for q in queries]

        query_vectors: list[list[float] | None]
        try:
            async with Embedder() as embedder:
                query_vectors = await embedder.embed_many(texts)
        except Exception:
            logger.warning("batch embed failed — falling back to per-query embedding")
            query_vectors = [None] * len(queries)

        bm25_vectors: list[dict[int, float] | None]
        try:
            from compass.search.common.bm25 import ensure_bm25_encoder

            enc = ensure_bm25_encoder()
            bm25_vectors = [enc.encode_query(t) for t in texts] if enc else [None] * len(queries)
        except Exception:
            logger.warning("batch BM25 encode failed — falling back")
            bm25_vectors = [None] * len(queries)

        sem = asyncio.Semaphore(concurrency)
        t_batch = time.perf_counter()

        async def search_one(idx: int) -> QueryResult:
            q = queries[idx]
            async with sem:
                t0 = time.perf_counter()
                try:
                    req = SearchRequest(
                        query=q.text,
                        top_k=top_k,
                        level=level,
                        query_vector=query_vectors[idx],
                        sparse_vector=bm25_vectors[idx],
                    )
                    result = await engine.search(req)
                    pool_ids = [h.arxiv_id for h in result.hits]
                except Exception:
                    logger.exception("search failed for query %s", q.id)
                    pool_ids = []

                elapsed = (time.perf_counter() - t0) * 1000
                pred_ids = pool_ids[:top_k]
                gt_set = set(q.gt_arxiv_ids)
                pred_set = set(pred_ids)

                return QueryResult(
                    query_id=q.id,
                    question=q.text,
                    gt_arxiv_ids=q.gt_arxiv_ids,
                    predicted_arxiv_ids=sorted(pred_set),
                    pool_arxiv_ids=pool_ids,
                    search_ms=round(elapsed, 1),
                    hit_ids=sorted(gt_set & pred_set),
                    missed_ids=sorted(gt_set - pred_set),
                )

        # Concurrent execution with periodic progress
        total = len(queries)
        results: list[QueryResult] = []
        pending: set[asyncio.Task] = {asyncio.create_task(search_one(idx)) for idx in range(total)}
        while pending:
            elapsed_sec = time.perf_counter() - t_batch
            logger.info(
                "progress %d/%d (%.0f s, %.1f q/s)",
                len(results),
                total,
                elapsed_sec,
                len(results) / elapsed_sec if results else 0,
            )

            done_set, pending = await asyncio.wait(pending, timeout=5)
            for f in done_set:
                results.append(f.result())

        elapsed_batch = time.perf_counter() - t_batch
        logger.info(
            "batch done — %d queries in %.0f s (%.1f q/s)",
            len(results),
            elapsed_batch,
            len(results) / elapsed_batch,
        )
        # Stable order = original query sequence (asyncio.gather preserves order)
        by_id = {r.query_id: r for r in results}
        return [by_id[q.id] for q in queries]

    # ── versioning ──────────────────────────────────────────────────────

    def _version_dir(self, version: str, level: int = 1) -> Path:
        d = self.output_root / f"l{level}" / version
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _latest_version_dir(self, level: int = 1) -> Path | None:
        base = self.output_root / f"l{level}"
        dirs = sorted(
            [d for d in base.glob("v*") if d.is_dir()],
            key=_version_key,
        )
        return dirs[-1] if dirs else None

    def _git_commit_hash(self) -> str:
        if self._git_commit is None:
            try:
                self._git_commit = subprocess.check_output(
                    ["git", "rev-parse", "--short", "HEAD"],
                    cwd=Path(__file__).resolve().parents[2],
                    text=True,
                ).strip()
            except Exception:
                self._git_commit = "unknown"
        return self._git_commit

    # ── save ────────────────────────────────────────────────────────────

    def _save(
        self,
        output_dir: Path,
        agg: dict[str, Any],
        results: list[QueryResult],
        params: dict[str, Any] | None = None,
    ) -> None:
        summary: dict[str, Any] = {
            "timestamp": datetime.datetime.now().astimezone().isoformat(),
            "git_commit": self._git_commit_hash(),
            "metrics": agg,
        }
        if params:
            summary["params"] = params
        with (output_dir / "results.json").open("w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

        per_query = [_result_dict(r) for r in results]
        with (output_dir / "per_query.json").open("w", encoding="utf-8") as f:
            json.dump(per_query, f, indent=2, ensure_ascii=False)


# ── helpers ──────────────────────────────────────────────────────────────


def _result_dict(r: QueryResult) -> dict[str, Any]:
    return {
        "query_id": r.query_id,
        "question": r.question,
        "gt_arxiv_ids": r.gt_arxiv_ids,
        "predicted_arxiv_ids": r.predicted_arxiv_ids,
        "pool_arxiv_ids": r.pool_arxiv_ids,
        "hit_ids": r.hit_ids,
        "missed_ids": r.missed_ids,
        "iou": round(r.iou, 6),
        "recall": round(r.recall, 6),
        "precision": round(r.precision, 6),
        "search_ms": r.search_ms,
    }


def _version_key(v: str | Path) -> tuple[int, int]:
    try:
        name = v.name if isinstance(v, Path) else str(v)
        parts = name.lstrip("v").split(".")
        return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except (ValueError, IndexError):
        return (0, 0)
