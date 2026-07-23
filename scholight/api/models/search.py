"""Public request and response models for the Scholight search API."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from scholight.models.search import SearchRequest

StrictString = Annotated[str, StringConstraints(strict=True)]
_CATEGORY_PATTERN = re.compile(r"[A-Za-z0-9.-]+")


class SearchStrength(StrEnum):
    """Public search quality presets."""

    STANDARD = "standard"
    THOROUGH = "thorough"


class PublicSearchFilters(BaseModel):
    """Filters supported by the public HTTP search contract."""

    model_config = ConfigDict(extra="forbid")

    categories: list[StrictString] = Field(default_factory=list, max_length=10)
    authors: list[StrictString] = Field(default_factory=list, max_length=10)
    date_from: date | None = None
    date_to: date | None = None

    @field_validator("categories")
    @staticmethod
    def _normalize_categories(values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = value.strip()
            if not 1 <= len(item) <= 32 or _CATEGORY_PATTERN.fullmatch(item) is None:
                raise ValueError("categories must contain valid arXiv category names")
            if item not in seen:
                seen.add(item)
                normalized.append(item)
        return normalized

    @field_validator("authors")
    @staticmethod
    def _normalize_authors(values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = value.strip()
            if not 1 <= len(item) <= 200:
                raise ValueError("authors must contain non-empty names of at most 200 characters")
            if item not in seen:
                seen.add(item)
                normalized.append(item)
        return normalized

    @model_validator(mode="after")
    def _validate_date_range(self) -> Self:
        if (
            self.date_from is not None
            and self.date_to is not None
            and self.date_from > self.date_to
        ):
            raise ValueError("date_from must be on or before date_to")
        return self


class PublicSearchRequest(BaseModel):
    """Stable public search input, isolated from internal pipeline controls."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "query": "retrieval augmented generation",
                    "strength": "standard",
                    "limit": 10,
                    "filters": {
                        "categories": ["cs.AI", "cs.IR"],
                        "authors": [],
                        "date_from": "2020-01-01",
                        "date_to": None,
                    },
                }
            ]
        },
    )

    query: StrictString
    strength: SearchStrength = SearchStrength.STANDARD
    limit: StrictInt = Field(default=10, ge=1, le=20)
    filters: PublicSearchFilters = Field(default_factory=PublicSearchFilters)

    @field_validator("query")
    @staticmethod
    def _normalize_query(value: str) -> str:
        query = value.strip()
        if not 1 <= len(query) <= 500:
            raise ValueError("query must contain between 1 and 500 characters after trimming")
        if any(unicodedata.category(char) == "Cc" and char not in "\t\n\r" for char in query):
            raise ValueError("query contains an unsupported control character")
        return query

    @field_validator("filters", mode="before")
    @staticmethod
    def _normalize_filters(value: object) -> object:
        return {} if value is None else value

    def to_internal(self) -> SearchRequest:
        """Map the public preset to fixed internal pipeline settings."""
        filters = self.filters
        return SearchRequest(
            query=self.query,
            level=1 if self.strength is SearchStrength.STANDARD else 2,
            top_k=self.limit,
            strategy=None,
            enable_fusion=False,
            categories=filters.categories or None,
            authors=filters.authors or None,
            date_from=filters.date_from.isoformat() if filters.date_from is not None else None,
            date_to=filters.date_to.isoformat() if filters.date_to is not None else None,
            arxiv_ids=None,
            query_vector=None,
            sparse_vector=None,
        )


class PublicSearchHit(BaseModel):
    """A ranked paper result without internal search diagnostics."""

    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1, description="Authoritative order within this response.")
    score: float = Field(
        allow_inf_nan=False,
        description=(
            "Unnormalized retrieval signal; compare only within the current response, never "
            "across queries, strengths, indexes, models, or time."
        ),
    )
    arxiv_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    authors: list[str]
    abstract: str | None
    categories: list[str]
    submitted_at: datetime | None
    updated_at: datetime | None
    version: int | None = Field(ge=1)
    arxiv_url: AnyHttpUrl
    pdf_url: AnyHttpUrl


class PublicSearchResponse(BaseModel):
    """Public search response; rank is authoritative and score is unnormalized."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "query": "retrieval augmented generation",
                    "strength": "standard",
                    "degraded": True,
                    "hits": [
                        {
                            "rank": 1,
                            "score": 12.75,
                            "arxiv_id": "2401.12345",
                            "title": "A Paper About Retrieval",
                            "authors": ["Example Author"],
                            "abstract": None,
                            "categories": ["cs.AI", "cs.IR"],
                            "submitted_at": "2024-01-20T00:00:00Z",
                            "updated_at": "2024-03-05T00:00:00Z",
                            "version": 2,
                            "arxiv_url": "https://arxiv.org/abs/2401.12345",
                            "pdf_url": "https://arxiv.org/pdf/2401.12345",
                        }
                    ],
                    "result_count": 1,
                    "elapsed_ms": 842.37,
                }
            ]
        },
    )

    query: str
    strength: SearchStrength
    degraded: bool
    hits: list[PublicSearchHit]
    result_count: int = Field(ge=0)
    elapsed_ms: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _validate_result_count(self) -> Self:
        if self.result_count != len(self.hits):
            raise ValueError("result_count must equal the number of hits")
        return self


__all__ = [
    "PublicSearchFilters",
    "PublicSearchHit",
    "PublicSearchRequest",
    "PublicSearchResponse",
    "SearchStrength",
]
