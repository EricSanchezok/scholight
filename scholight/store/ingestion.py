"""Narrow, non-destructive Zilliz interface for the ingestion scheduler.

This module deliberately exposes no collection lifecycle operations and no
filter-based deletes.  A revision is installed by upserting a complete new
chunk set before deleting only verified stale primary keys.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal, cast

from pymilvus.exceptions import MilvusException

from scholight.sources.arxiv import canonicalize_arxiv_id
from scholight.store.client import _WRITE_LOCK, QUERY_CONSISTENCY, batched, escape_sql, get_client
from scholight.store.ingest import StoreError, _validate_chunk_insert, upsert_arxiv_papers

MAX_PAPER_CHUNKS = 10_000
_RESOURCE_FLAGS = frozenset({"has_latex", "has_pdf", "has_markdown", "has_chunks"})
_INTERNAL_FIELDS = frozenset({"_version_available", "_metadata_fields"})
_PAPER_STATE_FIELDS = [
    "arxiv_id",
    "created",
    "updated",
    "version",
    "has_latex",
    "has_pdf",
    "has_markdown",
    "has_chunks",
]


class IngestionSafetyError(StoreError):
    """An ingestion write was rejected before destructive work could occur."""


@dataclass(frozen=True, slots=True)
class MetadataOutcome:
    arxiv_id: str
    target_version: int
    kind: Literal["new", "revision"] | None


def get_paper(arxiv_id: str) -> dict[str, Any] | None:
    canonical = canonicalize_arxiv_id(arxiv_id)
    if canonical is None or canonical != arxiv_id:
        raise IngestionSafetyError("Invalid canonical arXiv ID")
    try:
        rows = get_client().get(
            "arxiv_papers",
            ids=[arxiv_id],
            output_fields=_PAPER_STATE_FIELDS,
            consistency_level="Strong",
        )
    except MilvusException as exc:
        raise StoreError("Unable to read paper for ingestion") from exc
    return cast("dict[str, Any]", rows[0]) if rows else None


def paper_exists_on_date(date: str) -> bool:
    """Probe a date without scanning the full collection."""
    safe = escape_sql(date)
    try:
        rows = get_client().query(
            "arxiv_papers",
            filter=f"created == '{safe}' or updated == '{safe}'",
            output_fields=["arxiv_id"],
            limit=1,
            consistency_level=QUERY_CONSISTENCY,
        )
    except MilvusException as exc:
        raise StoreError("Unable to probe paper date") from exc
    return bool(rows)


def list_missing_chunks(from_date: str, to_date: str, limit: int) -> list[dict[str, Any]]:
    if not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")
    safe_from = escape_sql(from_date)
    safe_to = escape_sql(to_date)
    try:
        rows = get_client().query(
            "arxiv_papers",
            filter=(f"has_chunks == false and created >= '{safe_from}' and created <= '{safe_to}'"),
            output_fields=["arxiv_id", "version", "created"],
            limit=limit,
            consistency_level=QUERY_CONSISTENCY,
        )
    except MilvusException as exc:
        raise StoreError("Unable to list papers missing chunks") from exc
    return cast("list[dict[str, Any]]", rows)


def write_metadata_papers(papers: list[dict[str, Any]]) -> list[MetadataOutcome]:
    """Insert new papers and partially enrich existing rows without flag loss."""
    if not papers:
        return []
    client = get_client()
    ids = [str(p["arxiv_id"]) for p in papers]
    try:
        existing_rows = client.get(
            "arxiv_papers",
            ids=ids,
            output_fields=_PAPER_STATE_FIELDS,
            consistency_level="Strong",
        )
    except MilvusException as exc:
        raise StoreError("Unable to read existing paper metadata") from exc
    existing = {str(row["arxiv_id"]): row for row in existing_rows}

    new_rows: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    outcomes: list[MetadataOutcome] = []
    for paper in papers:
        arxiv_id = str(paper["arxiv_id"])
        canonical = canonicalize_arxiv_id(arxiv_id)
        if canonical is None or canonical != arxiv_id:
            raise IngestionSafetyError("Metadata contains an invalid arXiv ID")
        current = existing.get(arxiv_id)
        incoming_version = max(int(paper.get("version") or 1), 1)
        version_available = bool(paper.get("_version_available"))
        if current is None:
            new_rows.append({k: v for k, v in paper.items() if k not in _INTERNAL_FIELDS})
            outcomes.append(MetadataOutcome(arxiv_id, incoming_version, "new"))
            continue

        stored_version = max(int(current.get("version") or 1), 1)
        if version_available and incoming_version < stored_version:
            outcomes.append(MetadataOutcome(arxiv_id, stored_version, None))
            continue
        is_revision = version_available and incoming_version > stored_version
        available_fields = paper.get("_metadata_fields")
        update: dict[str, Any] = {"arxiv_id": arxiv_id}
        for key, value in paper.items():
            if key in _RESOURCE_FLAGS or key in _INTERNAL_FIELDS or key == "arxiv_id":
                continue
            if available_fields is not None and key not in available_fields:
                continue
            if value in (None, "", [], {}):
                continue
            if key == "version" and (not version_available or incoming_version < stored_version):
                continue
            update[key] = value
        if len(update) > 1:
            updates.append(update)
        outcomes.append(
            MetadataOutcome(
                arxiv_id,
                incoming_version if is_revision else stored_version,
                "revision" if is_revision else None,
            )
        )

    if new_rows:
        upsert_arxiv_papers(new_rows)
    updates_by_fields: defaultdict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for update in updates:
        updates_by_fields[tuple(sorted(update))].append(update)
    try:
        with _WRITE_LOCK:
            for homogeneous_updates in updates_by_fields.values():
                for batch in batched(homogeneous_updates):
                    client.upsert(
                        "arxiv_papers",
                        data=batch,
                        partial_update=True,
                        consistency_level="Strong",
                    )
    except MilvusException as exc:
        raise StoreError("Unable to update paper metadata") from exc
    return outcomes


def get_chunk_ids(arxiv_id: str) -> set[str]:
    """Return verified primary keys for one paper, failing closed over 10k."""
    canonical = canonicalize_arxiv_id(arxiv_id)
    if canonical is None or canonical != arxiv_id:
        raise IngestionSafetyError("Invalid canonical arXiv ID")
    safe_id = escape_sql(arxiv_id)
    try:
        rows = get_client().query(
            "arxiv_chunks",
            filter=f"arxiv_id == '{safe_id}'",
            output_fields=["chunk_id", "arxiv_id"],
            limit=MAX_PAPER_CHUNKS + 1,
            consistency_level="Strong",
        )
    except MilvusException as exc:
        raise StoreError("Unable to read existing chunk IDs") from exc
    if len(rows) > MAX_PAPER_CHUNKS:
        raise IngestionSafetyError("Paper exceeds the safe chunk replacement limit")
    ids: set[str] = set()
    for row in rows:
        if row.get("arxiv_id") != arxiv_id:
            raise IngestionSafetyError("Chunk query returned a mismatched arXiv ID")
        chunk_id = row.get("chunk_id")
        if not isinstance(chunk_id, str) or not chunk_id:
            raise IngestionSafetyError("Chunk query returned an invalid primary key")
        ids.add(chunk_id)
    return ids


def install_paper_chunks(
    arxiv_id: str,
    chunks: list[dict[str, Any]],
    *,
    target_version: int,
    resource_flags: dict[str, bool],
) -> None:
    """Install one complete revision using an upsert-first, exact-delete order."""
    if not chunks or len(chunks) > MAX_PAPER_CHUNKS:
        raise IngestionSafetyError("Chunk set must contain between 1 and 10000 chunks")
    new_ids: set[str] = set()
    for chunk in chunks:
        _validate_chunk_insert(chunk)
        if chunk.get("arxiv_id") != arxiv_id:
            raise IngestionSafetyError("Chunk set contains another paper")
        chunk_id = str(chunk["chunk_id"])
        if chunk_id in new_ids:
            raise IngestionSafetyError("Chunk set contains duplicate primary keys")
        new_ids.add(chunk_id)

    old_ids = get_chunk_ids(arxiv_id)
    client = get_client()
    try:
        with _WRITE_LOCK:
            current = client.get(
                "arxiv_papers",
                ids=[arxiv_id],
                output_fields=["arxiv_id", "version"],
                consistency_level="Strong",
            )
            if not current or max(int(current[0].get("version") or 1), 1) != target_version:
                raise IngestionSafetyError("Paper version changed before chunk installation")
            for chunk_batch in batched(chunks):
                client.upsert("arxiv_chunks", data=chunk_batch, consistency_level="Strong")
            stale_ids = sorted(old_ids - new_ids)
            for id_batch in batched(stale_ids):
                client.delete(
                    "arxiv_chunks",
                    ids=id_batch,
                    consistency_level="Strong",
                )
            fields = {key: bool(value) for key, value in resource_flags.items()}
            if not set(fields) <= _RESOURCE_FLAGS:
                raise IngestionSafetyError("Unknown paper resource flag")
            fields["has_chunks"] = True
            client.upsert(
                "arxiv_papers",
                data=[{"arxiv_id": arxiv_id, **fields}],
                partial_update=True,
                consistency_level="Strong",
            )
    except MilvusException as exc:
        raise StoreError("Unable to install paper chunks") from exc
