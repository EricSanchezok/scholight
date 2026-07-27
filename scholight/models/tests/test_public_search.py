"""Public HTTP search contract tests."""

from __future__ import annotations

import warnings
from datetime import UTC, datetime

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError

from scholight.api.models.search import (
    PublicSearchFilters,
    PublicSearchHit,
    PublicSearchRequest,
    PublicSearchResponse,
    SearchStrength,
)


@pytest.mark.parametrize("strength", [SearchStrength.STANDARD, SearchStrength.THOROUGH])
def test_public_search_request_accepts_strength(strength: SearchStrength) -> None:
    request = PublicSearchRequest(query="  retrieval augmented generation  ", strength=strength)

    assert (request.query, request.strength, request.limit) == (
        "retrieval augmented generation",
        strength,
        10,
    )


def test_public_search_request_normalizes_nullable_filters() -> None:
    request = PublicSearchRequest.model_validate({"query": "test", "filters": None})

    assert request.filters == PublicSearchFilters()


@pytest.mark.parametrize(
    "field",
    [
        "level",
        "top_k",
        "strategy",
        "enable_fusion",
        "query_vector",
        "sparse_vector",
        "arxiv_ids",
    ],
)
def test_public_search_request_rejects_internal_pipeline_fields(field: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        PublicSearchRequest.model_validate({"query": "test", field: 1})

    assert exc_info.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize("query", ["", "   ", "test\0query", "test\x07query"])
def test_public_search_request_rejects_invalid_query(query: str) -> None:
    with pytest.raises(ValidationError):
        PublicSearchRequest(query=query)


@pytest.mark.parametrize("limit", [True, 1.0, "10", 0, 51])
def test_public_search_request_enforces_strict_limit_bounds(limit: object) -> None:
    with pytest.raises(ValidationError):
        PublicSearchRequest(query="test", limit=limit)  # type: ignore[arg-type]


def test_public_search_request_accepts_fifty_results() -> None:
    request = PublicSearchRequest(query="test", limit=50)

    assert request.limit == 50


def test_public_search_filters_trim_deduplicate_and_preserve_order() -> None:
    filters = PublicSearchFilters(
        categories=[" cs.AI ", "cs.IR", "cs.AI"],
        authors=[" Ada Lovelace ", "Alan Turing", "Ada Lovelace"],
    )

    assert filters.categories == ["cs.AI", "cs.IR"]
    assert filters.authors == ["Ada Lovelace", "Alan Turing"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("categories", [""]),
        ("categories", ["cs.AI!"]),
        ("categories", [f"cs.{('A' * 30)}"]),
        ("authors", ["  "]),
        ("authors", ["A" * 201]),
    ],
)
def test_public_search_filters_reject_invalid_values(field: str, value: list[str]) -> None:
    with pytest.raises(ValidationError):
        PublicSearchFilters.model_validate({field: value})


def test_public_search_filters_reject_inverted_date_range() -> None:
    with pytest.raises(ValidationError):
        PublicSearchFilters.model_validate({"date_from": "2025-01-02", "date_to": "2025-01-01"})


def test_public_request_maps_to_fixed_internal_search_request() -> None:
    public = PublicSearchRequest.model_validate(
        {
            "query": "test",
            "strength": "thorough",
            "limit": 20,
            "filters": {
                "categories": ["cs.AI"],
                "authors": ["Ada Lovelace"],
                "date_from": "2020-01-01",
                "date_to": "2025-01-01",
            },
        }
    )

    internal = public.to_internal()

    assert internal.model_dump() == {
        "query": "test",
        "top_k": 20,
        "level": 2,
        "enable_fusion": False,
        "strategy": None,
        "date_from": "2020-01-01",
        "date_to": "2025-01-01",
        "categories": ["cs.AI"],
        "authors": ["Ada Lovelace"],
        "arxiv_ids": None,
        "query_vector": None,
        "sparse_vector": None,
    }


def test_public_search_hit_serializes_raw_score_null_abstract_and_urls() -> None:
    hit = PublicSearchHit.model_validate(
        {
            "rank": 1,
            "score": 12.75,
            "arxiv_id": "2401.12345",
            "title": "A Paper",
            "authors": ["Author"],
            "abstract": None,
            "categories": ["cs.AI"],
            "submitted_at": datetime(2024, 1, 20, tzinfo=UTC),
            "updated_at": datetime(2024, 3, 5, tzinfo=UTC),
            "version": 2,
            "arxiv_url": "https://arxiv.org/abs/2401.12345",
            "pdf_url": "https://arxiv.org/pdf/2401.12345",
        }
    )

    dumped = hit.model_dump(mode="json")

    assert dumped["score"] == 12.75
    assert dumped["abstract"] is None
    assert dumped["arxiv_url"] == "https://arxiv.org/abs/2401.12345"
    assert dumped["pdf_url"] == "https://arxiv.org/pdf/2401.12345"


def test_public_search_hit_accepts_missing_date_and_version_metadata() -> None:
    hit = PublicSearchHit.model_validate(
        {
            "rank": 1,
            "score": 12.75,
            "arxiv_id": "2401.12345",
            "title": "A Paper",
            "authors": [],
            "abstract": None,
            "categories": [],
            "submitted_at": None,
            "updated_at": None,
            "version": None,
            "arxiv_url": "https://arxiv.org/abs/2401.12345",
            "pdf_url": "https://arxiv.org/pdf/2401.12345",
        }
    )

    assert (hit.submitted_at, hit.updated_at, hit.version) == (None, None, None)


def test_public_search_response_excludes_internal_diagnostics() -> None:
    response = PublicSearchResponse(
        query="test",
        strength=SearchStrength.STANDARD,
        degraded=False,
        hits=[],
        result_count=0,
        elapsed_ms=1.25,
    )

    assert response.model_dump().keys() == {
        "query",
        "strength",
        "degraded",
        "hits",
        "result_count",
        "elapsed_ms",
    }


def test_public_search_schema_documents_examples_and_score_semantics() -> None:
    request_schema = PublicSearchRequest.model_json_schema()
    hit_schema = PublicSearchHit.model_json_schema()
    response_schema = PublicSearchResponse.model_json_schema()

    assert request_schema["examples"][0] == {
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
    assert "authoritative" in hit_schema["properties"]["rank"]["description"].lower()
    assert "current response" in hit_schema["properties"]["score"]["description"].lower()
    assert response_schema["examples"][0]["hits"][0]["abstract"] is None


def test_search_route_uses_only_public_contract_models() -> None:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="'asyncio.iscoroutinefunction' is deprecated.*",
            category=DeprecationWarning,
        )
        from scholight.api.routes.search import router

    route = next(route for route in router.routes if getattr(route, "path", None) == "")
    assert isinstance(route, APIRoute)
    assert route.body_field is not None

    assert route.body_field.field_info.annotation is PublicSearchRequest
    assert route.response_model is PublicSearchResponse
