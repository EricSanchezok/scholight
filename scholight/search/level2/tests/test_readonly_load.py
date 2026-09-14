"""Python SDK loaded enums must never cause collection management from a search."""

from unittest.mock import Mock

import pytest
from pymilvus.client.types import LoadState

from scholight.search.level2 import phases


@pytest.mark.parametrize("state", [LoadState.Loaded, "LoadStateLoaded"])
def test_loaded_state_accepts_sdk_and_rest_without_load(
    monkeypatch: pytest.MonkeyPatch, state: object
) -> None:
    client = Mock()
    client.get_load_state.return_value = {"state": state}
    monkeypatch.setattr(phases, "get_client", lambda: client)
    monkeypatch.setattr(phases, "_CHUNK_LOADED", False)
    phases._ensure_chunks_loaded()
    client.load_collection.assert_not_called()


def test_unloaded_collection_fails_without_implicit_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Mock()
    client.get_load_state.return_value = {"state": LoadState.NotLoad}
    monkeypatch.setattr(phases, "get_client", lambda: client)
    monkeypatch.setattr(phases, "_CHUNK_LOADED", False)
    with pytest.raises(RuntimeError, match="not loaded"):
        phases._ensure_chunks_loaded()
    client.load_collection.assert_not_called()
