"""Read-only validation of the collections needed by public search."""

from pymilvus import DataType, MilvusClient
from pymilvus.client.types import LoadState

from scholight.config import public_search_modes, settings


def inspect_search_collections(client: MilvusClient, *, timeout: float) -> None:
    """Raise on incompatible search dependencies; never perform collection DDL."""
    names = ["arxiv_papers"]
    if "thorough" in public_search_modes():
        names.append("arxiv_chunks")
    for name in names:
        chunk = name == "arxiv_chunks"
        primary = "chunk_id" if chunk else "arxiv_id"
        dense = "content_embedding" if chunk else "abstract_embedding"
        sparse = "content_bm25" if chunk else "abstract_bm25"
        fields = {
            field["name"]: field
            for field in client.describe_collection(name, timeout=timeout)["fields"]
        }
        if not fields.get(primary, {}).get("is_primary"):
            raise ValueError(f"{name} primary key is incompatible")
        vector = fields.get(dense, {})
        if (
            vector.get("type") != DataType.FLOAT_VECTOR
            or int(vector.get("params", {}).get("dim", 0)) != settings.embedding_dim
        ):
            raise ValueError(f"{name} embedding dimension or type is incompatible")
        if fields.get(sparse, {}).get("type") != DataType.SPARSE_FLOAT_VECTOR:
            raise ValueError(f"{name} sparse vector is incompatible")
        indexes = {
            index["field_name"]: index
            for index in (
                client.describe_index(name, index_name, timeout=timeout)
                for index_name in client.list_indexes(name, timeout=timeout)
            )
        }
        for field, metric in ((dense, "COSINE"), (sparse, "BM25")):
            index = indexes.get(field, {})
            if index.get("state") != "Finished" or index.get("metric_type") != metric:
                raise ValueError(f"{name} search index is unavailable or incompatible")
        state = client.get_load_state(name, timeout=timeout).get("state")
        if state != LoadState.Loaded and state != "LoadStateLoaded":
            raise ValueError(f"{name} is not loaded")
