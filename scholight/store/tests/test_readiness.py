"""Search readiness inspects only its own required collections and never repairs them."""

from unittest.mock import Mock

import pytest
from pymilvus import DataType
from pymilvus.client.types import LoadState

from scholight.config import settings
from scholight.store.readiness import inspect_search_collections


def client_stub() -> Mock:
    client = Mock()

    def describe(name: str, **kwargs: object) -> dict[str, object]:
        chunk = name == "arxiv_chunks"
        vector = "content_embedding" if chunk else "abstract_embedding"
        sparse = "content_bm25" if chunk else "abstract_bm25"
        return {
            "fields": [
                {
                    "name": "chunk_id" if chunk else "arxiv_id",
                    "type": DataType.VARCHAR,
                    "is_primary": True,
                },
                {
                    "name": vector,
                    "type": DataType.FLOAT_VECTOR,
                    "params": {"dim": settings.embedding_dim},
                },
                {"name": sparse, "type": DataType.SPARSE_FLOAT_VECTOR},
            ]
        }

    client.describe_collection.side_effect = describe
    client.get_load_state.return_value = {"state": LoadState.Loaded}
    client.list_indexes.side_effect = lambda name, **kw: (
        ["content_embedding", "content_bm25"]
        if name == "arxiv_chunks"
        else ["abstract_embedding", "abstract_bm25"]
    )
    client.describe_index.side_effect = lambda name, index, **kw: {
        "field_name": index,
        "state": "Finished",
        "metric_type": "BM25" if index.endswith("bm25") else "COSINE",
    }
    return client


def test_standard_does_not_inspect_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "public_thorough_enabled", False)
    client = client_stub()
    inspect_search_collections(client, timeout=2)
    assert [call.args[0] for call in client.describe_collection.call_args_list] == ["arxiv_papers"]
    client.load_collection.assert_not_called()
    client.create_collection.assert_not_called()


def test_wrong_dimension_fails_without_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    client = client_stub()
    describe = client.describe_collection.side_effect

    def wrong(name: str, **kw: object) -> dict[str, object]:
        schema = describe(name, **kw)
        schema["fields"][1]["params"]["dim"] = settings.embedding_dim + 1
        return schema

    client.describe_collection.side_effect = wrong
    with pytest.raises(ValueError, match="dimension"):
        inspect_search_collections(client, timeout=2)
    client.create_index.assert_not_called()


def test_thorough_requires_finished_chunk_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "runtime_profile", "full")
    monkeypatch.setattr(settings, "public_thorough_enabled", True)
    client = client_stub()
    describe = client.describe_index.side_effect
    client.describe_index.side_effect = lambda name, index, **kw: (
        describe(name, index, **kw) | ({"state": "Failed"} if name == "arxiv_chunks" else {})
    )
    with pytest.raises(ValueError, match="index"):
        inspect_search_collections(client, timeout=2)
    client.create_index.assert_not_called()


def test_inspection_stops_at_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    from scholight.store import readiness

    now = [0.0]
    monkeypatch.setattr(readiness.time, "monotonic", lambda: now[0])
    client = client_stub()
    describe = client.describe_collection.side_effect

    def delayed(name: str, **kw: object) -> dict[str, object]:
        now[0] = 3.0
        return describe(name, **kw)

    client.describe_collection.side_effect = delayed
    with pytest.raises(TimeoutError):
        inspect_search_collections(client, timeout=2)
    client.list_indexes.assert_not_called()
