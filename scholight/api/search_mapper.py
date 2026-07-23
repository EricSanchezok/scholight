"""Mapping from internal search results to the public HTTP contract."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time

from pydantic import AnyHttpUrl

from scholight.api.models.search import PublicSearchHit, PublicSearchResponse, SearchStrength
from scholight.models.search import SearchHit, SearchResult


def _utc_midnight(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return datetime.combine(parsed, time.min, tzinfo=UTC)


def _map_hit(
    hit: SearchHit,
    abstracts: Mapping[str, str | None] | None,
) -> PublicSearchHit:
    abstract = hit.abstract or None if abstracts is None else abstracts.get(hit.arxiv_id)
    return PublicSearchHit(
        rank=hit.rank,
        score=hit.score,
        arxiv_id=hit.arxiv_id,
        title=hit.title,
        authors=hit.authors,
        abstract=abstract,
        categories=hit.categories,
        submitted_at=_utc_midnight(hit.created),
        updated_at=_utc_midnight(hit.updated),
        version=hit.version,
        arxiv_url=AnyHttpUrl(f"https://arxiv.org/abs/{hit.arxiv_id}"),
        pdf_url=AnyHttpUrl(f"https://arxiv.org/pdf/{hit.arxiv_id}"),
    )


def map_search_response(
    result: SearchResult,
    *,
    strength: SearchStrength,
    elapsed_ms: float,
    degraded: bool = False,
    abstracts: Mapping[str, str | None] | None = None,
) -> PublicSearchResponse:
    """Preserve core order while joining optional public abstract metadata."""
    hits = [_map_hit(hit, abstracts) for hit in result.hits]
    return PublicSearchResponse(
        query=result.query,
        strength=strength,
        degraded=degraded,
        hits=hits,
        result_count=len(hits),
        elapsed_ms=elapsed_ms,
    )


__all__ = ["map_search_response"]
