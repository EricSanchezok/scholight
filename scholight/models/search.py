"""Search pipeline data models — request, response, and timing diagnostics."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    """Search parameters — the single source of truth for all pipeline control.

    The ``level`` field acts as a preset that the CLI or API caller sets; the
    individual ``enable_*`` flags are *defaulted* from that level but can be
    overridden per-request for surgical control of each pipeline stage.

    When ``query_vector`` is provided, the SearchEngine skips Phase 1
    (embedding) and uses it directly.  ``sparse_vector`` is the pre-computed
    BM25 sparse vector for hybrid search.
    """

    # ── Core ──
    query: str
    top_k: int = 10
    level: int = Field(default=1, ge=1, le=3)  # 1=paper, 2=+chunks, 3=+figures/tables

    # ── Pipeline stage toggles (default: all OFF — caller opts in) ──
    enable_fusion: bool = False

    # ── Named strategy (overrides enable_fusion when set) ──
    strategy: str | None = None

    # ── Filters ──
    date_from: str | None = None
    date_to: str | None = None
    categories: list[str] | None = None
    authors: list[str] | None = None
    arxiv_ids: list[str] | None = None

    # ── Pre-computed vectors (inject via API, not CLI) ──
    query_vector: list[float] | None = None
    sparse_vector: dict[int, float] | None = None


class SearchHit(BaseModel):
    """A single search result with complete paper metadata."""

    rank: int
    score: float
    arxiv_id: str
    title: str
    authors: list[str]
    abstract: str
    categories: list[str]
    created: str
    updated: str
    version: int
    updated_history: list[str]
    license: str
    comments: str
    doi: str
    journal_ref: str
    acm_class: str

    # ── Chunk evidence (Level 2 only) ──
    chunks: list[dict[str, str | float | int]] = Field(default_factory=list)

    @property
    def url(self) -> str:
        """arXiv abstract page URL for this paper."""
        return f"https://arxiv.org/abs/{self.arxiv_id}"


class PhaseTiming(BaseModel):
    """Timing breakdown for a single search phase."""

    phase: str
    duration_ms: float
    metadata: dict[str, int | str] = Field(default_factory=dict)


class SearchStats(BaseModel):
    """Aggregated statistics about the search execution."""

    level: int
    embedding_model: str
    embedding_dim: int
    paper_candidates: int
    phases: list[PhaseTiming]


class SearchResult(BaseModel):
    """Top-level search response returned to callers."""

    query: str
    level: int
    total_ms: float
    hits: list[SearchHit]
    stats: SearchStats | None = None
    total_papers: int | None = None
    total_chunks: int | None = None


__all__ = ["SearchRequest", "SearchHit", "PhaseTiming", "SearchStats", "SearchResult"]
