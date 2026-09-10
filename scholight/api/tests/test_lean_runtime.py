"""Public lean defaults and rejection before any paid work."""

import pytest
from pydantic import ValidationError

from scholight.api.models.search import PublicSearchRequest
from scholight.config import Settings, active_collections, require_full_runtime, settings


def test_default_runtime_is_lean() -> None:
    assert Settings.model_validate({}).runtime_profile == "lean"


def test_lean_only_requires_papers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "runtime_profile", "lean")
    assert active_collections() == ("arxiv_papers",)


def test_full_runtime_retains_both_collections(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "runtime_profile", "full")
    assert active_collections() == ("arxiv_papers", "arxiv_chunks")


def test_lean_rejects_fulltext_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "runtime_profile", "lean")
    with pytest.raises(ValueError, match="RUNTIME_PROFILE=full"):
        require_full_runtime("Full-text ingestion")


def test_public_thorough_rejected_in_full_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "runtime_profile", "full")
    with pytest.raises(ValidationError):
        PublicSearchRequest.model_validate({"query": "retrieval", "strength": "thorough"})


def test_legacy_standard_still_maps_to_abstract_search() -> None:
    assert (
        PublicSearchRequest.model_validate({"query": "retrieval", "strength": "standard"})
        .to_internal()
        .level
        == 1
    )
