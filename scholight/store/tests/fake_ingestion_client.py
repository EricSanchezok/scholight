"""High-fidelity in-memory substitute for the narrow ingestion store boundary."""

from __future__ import annotations

import re
import threading
from copy import deepcopy
from typing import Any

from pymilvus.exceptions import MilvusException


class FakeIngestionClient:
    """Model only operations allowed during ingestion and record their order."""

    def __init__(self) -> None:
        self.papers: dict[str, dict[str, Any]] = {}
        self.chunks: dict[str, dict[str, Any]] = {}
        self.operations: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_next_chunk_upsert = False
        self.fail_chunk_upsert_call: int | None = None
        self.fail_next_chunk_delete = False
        self._chunk_upsert_calls = 0
        self._lock = threading.Lock()

    @staticmethod
    def _select(row: dict[str, Any], fields: list[str] | None) -> dict[str, Any]:
        if fields is None:
            return deepcopy(row)
        return {field: deepcopy(row[field]) for field in fields if field in row}

    def get(
        self,
        collection_name: str,
        ids: list[str],
        *,
        output_fields: list[str] | None = None,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        with self._lock:
            source = self.papers if collection_name == "arxiv_papers" else self.chunks
            self.operations.append(("get", collection_name, {"ids": list(ids)}))
            return [
                self._select(source[item_id], output_fields) for item_id in ids if item_id in source
            ]

    def query(
        self,
        collection_name: str,
        *,
        filter: str,
        output_fields: list[str] | None = None,
        limit: int | None = None,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        with self._lock:
            self.operations.append(("query", collection_name, {"filter": filter}))
            if collection_name == "arxiv_chunks":
                match = re.fullmatch(r"arxiv_id == '([^']+)'", filter)
                if match is None:
                    raise AssertionError(f"unexpected chunk query: {filter}")
                rows = [row for row in self.chunks.values() if row["arxiv_id"] == match.group(1)]
            elif "has_chunks == false" in filter:
                dates = re.findall(r"'(\d{4}-\d{2}-\d{2})'", filter)
                if len(dates) != 2:
                    raise AssertionError(f"unexpected reconciliation query: {filter}")
                rows = [
                    row
                    for row in self.papers.values()
                    if not row.get("has_chunks")
                    and dates[0] <= str(row.get("created") or "") <= dates[1]
                ]
            else:
                dates = re.findall(r"'(\d{4}-\d{2}-\d{2})'", filter)
                if len(dates) != 2 or dates[0] != dates[1]:
                    raise AssertionError(f"unexpected paper query: {filter}")
                rows = [
                    row
                    for row in self.papers.values()
                    if row.get("created") == dates[0] or row.get("updated") == dates[0]
                ]
            selected = [self._select(row, output_fields) for row in rows]
            return selected[:limit] if limit is not None else selected

    def upsert(
        self,
        collection_name: str,
        *,
        data: list[dict[str, Any]],
        partial_update: bool = False,
        **_kwargs: Any,
    ) -> dict[str, int]:
        with self._lock:
            self.operations.append(
                (
                    "upsert",
                    collection_name,
                    {"count": len(data), "partial_update": partial_update},
                )
            )
            if collection_name == "arxiv_chunks":
                self._chunk_upsert_calls += 1
                should_fail = self.fail_next_chunk_upsert or (
                    self.fail_chunk_upsert_call == self._chunk_upsert_calls
                )
                if should_fail:
                    self.fail_next_chunk_upsert = False
                    raise MilvusException(message="injected chunk upsert failure", code=1)
            target = self.papers if collection_name == "arxiv_papers" else self.chunks
            primary_key = "arxiv_id" if collection_name == "arxiv_papers" else "chunk_id"
            for item in data:
                item_id = str(item[primary_key])
                if partial_update and item_id in target:
                    target[item_id].update(deepcopy(item))
                else:
                    target[item_id] = deepcopy(item)
            return {"upsert_count": len(data)}

    def delete(
        self,
        collection_name: str,
        *,
        ids: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, int]:
        if ids is None or "filter" in kwargs:
            raise AssertionError("ingestion deletes must use explicit primary-key ids")
        with self._lock:
            self.operations.append(("delete", collection_name, {"ids": list(ids)}))
            if collection_name != "arxiv_chunks":
                raise AssertionError("ingestion may delete only arxiv_chunks")
            if self.fail_next_chunk_delete:
                self.fail_next_chunk_delete = False
                raise MilvusException(message="injected chunk delete failure", code=1)
            deleted = 0
            for item_id in ids:
                if self.chunks.pop(item_id, None) is not None:
                    deleted += 1
            return {"delete_count": deleted}
