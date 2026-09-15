"""Bounded, write-ahead fulltext replacement; no collection lifecycle operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import shutil
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Iterable
from functools import partial
from pathlib import Path
from typing import Any, TypeVar, cast
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from scholight.models.ingestion_target import digest_json
from scholight.store.archive_io import ArchiveLocation, file_digest
from scholight.store.client import _WRITE_LOCK, escape_sql
from scholight.store.ingestion import MAX_PAPER_CHUNKS

T = TypeVar("T")
_FIELDS = ["chunk_id", "arxiv_id", "chunk_idx", "content_text", "content_embedding"]
_FLAGS = {"has_latex", "has_pdf", "has_markdown", "has_chunks"}
_BATCH = 64


async def settled_io(operation: Callable[[], T]) -> T:
    """Do not abandon an in-flight write thread when the enclosing task is cancelled."""
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


async def _settled_write(operation: Callable[[], T]) -> T:
    # The SDK singleton allows concurrent reads, but its writes must share the
    # same lock used by other store entry points. Never block the asyncio loop.
    def locked() -> T:
        with _WRITE_LOCK:
            return operation()

    return await settled_io(locked)


def chunk_digest(row: dict[str, Any], dimension: int) -> str:
    vector = row["content_embedding"]
    if len(vector) != dimension or not all(math.isfinite(float(v)) for v in vector):
        raise ValueError("Invalid fulltext vector dimension or value")
    scalars = {key: row[key] for key in _FIELDS if key != "content_embedding"}
    raw = json.dumps(scalars, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    vector_bytes = pa.array(vector, type=pa.float32()).buffers()[1].to_pybytes()
    return hashlib.sha256(raw + b"\0" + vector_bytes).hexdigest()


async def _iterate(
    rows: Iterable[dict[str, Any]] | AsyncIterable[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    if isinstance(rows, AsyncIterable):
        async for row in rows:
            yield row
    else:
        for row in rows:
            yield row


class FulltextInstall:
    """Caller holds a per-paper database advisory lock and supplies a live lease guard."""

    def __init__(
        self,
        client: Any,
        *,
        target_id: str,
        arxiv_id: str,
        version: int,
        profile: str,
        dimension: int,
        location: str,
        workspace: Path,
        guard: Callable[[], Awaitable[None]],
        record: Callable[[str, dict[str, Any]], Awaitable[None]],
        s3_client: Any = None,
    ) -> None:
        self.client = client
        self.identity: dict[str, Any] = {
            "target_id": target_id,
            "arxiv_id": arxiv_id,
            "version": version,
            "profile": profile,
            "dimension": dimension,
        }
        self.location = ArchiveLocation(location, s3_client=s3_client)
        self.uri = location
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.guard = guard
        self.record = record

    async def exists(self) -> bool:
        return await settled_io(lambda: self.location.exists("manifest.json"))

    def _save(self, rows: list[dict[str, Any]], prefix: str) -> dict[str, Any]:
        if shutil.disk_usage(self.workspace).free < 128 * 1024**2:
            raise OSError("Insufficient temporary disk space for fulltext recovery")
        schema = pa.schema(
            [
                ("pk", pa.string()),
                ("vector", pa.list_(pa.float32(), self.identity["dimension"])),
                ("payload", pa.binary()),
            ]
        )
        encoded = []
        digests = {}
        for row in rows:
            if row["arxiv_id"] != self.identity["arxiv_id"] or not isinstance(row["chunk_id"], str):
                raise ValueError("Fulltext shard contains an unexpected paper or primary key")
            pk = row["chunk_id"]
            if pk in digests:
                raise ValueError("Duplicate fulltext primary key")
            digests[pk] = chunk_digest(row, self.identity["dimension"])
            encoded.append(
                {
                    "pk": pk,
                    "vector": row["content_embedding"],
                    "payload": json.dumps(
                        {k: row[k] for k in _FIELDS if k != "content_embedding"},
                        sort_keys=True,
                        ensure_ascii=False,
                    ).encode(),
                }
            )
        path = self.workspace / "shard.parquet"
        pq.write_table(pa.Table.from_pylist(encoded, schema=schema), path, compression="zstd")
        name = f"{prefix}-{uuid4().hex}.parquet"
        digest = file_digest(path)
        self.location.upload(path, name)
        self.location.download(name, path)
        if file_digest(path) != digest:
            raise ValueError("Uploaded fulltext shard checksum mismatch")
        path.unlink()
        return {"name": name, "sha256": digest, "rows": len(rows), "digests": digests}

    def _load(self, shard: dict[str, Any]) -> list[dict[str, Any]]:
        path = self.workspace / "read.parquet"
        self.location.download(shard["name"], path)
        if file_digest(path) != shard["sha256"]:
            raise ValueError("Fulltext archive checksum mismatch")
        table = pq.read_table(path)
        path.unlink()
        rows = []
        for item in table.to_pylist():
            row = json.loads(item["payload"])
            row["content_embedding"] = item["vector"]
            if row["chunk_id"] != item["pk"] or row["arxiv_id"] != self.identity["arxiv_id"]:
                raise ValueError("Fulltext archive identity mismatch")
            rows.append(row)
        if (
            len(rows) != shard["rows"]
            or {r["chunk_id"]: chunk_digest(r, self.identity["dimension"]) for r in rows}
            != shard["digests"]
        ):
            raise ValueError("Fulltext archive verification failed")
        return rows

    async def _paper(self) -> dict[str, Any]:
        await self.guard()
        rows = await settled_io(
            lambda: self.client.get(
                "arxiv_papers",
                ids=[self.identity["arxiv_id"]],
                output_fields=["arxiv_id", "version", *_FLAGS],
                consistency_level="Strong",
                timeout=45,
            )
        )
        if len(rows) != 1 or rows[0]["version"] != self.identity["version"]:
            raise ValueError("Paper version changed during fulltext installation")
        return cast("dict[str, Any]", rows[0])

    async def prepare(
        self,
        chunks: Iterable[dict[str, Any]] | AsyncIterable[dict[str, Any]],
        flags: dict[str, bool],
    ) -> dict[str, Any]:
        if await self.exists():
            return await self._manifest()
        if not set(flags) <= _FLAGS:
            raise ValueError("Unknown fulltext resource flags")
        paper = await self._paper()
        new = []
        batch = []
        ids = set()
        async for row in _iterate(chunks):
            if row["chunk_id"] in ids:
                raise ValueError("Duplicate fulltext primary key")
            ids.add(row["chunk_id"])
            if len(ids) > MAX_PAPER_CHUNKS:
                raise ValueError("Fulltext exceeds per-paper chunk bound")
            batch.append(row)
            if len(batch) == _BATCH:
                await self.guard()
                new.append(await settled_io(partial(self._save, batch, "new")))
                batch = []
        if batch:
            new.append(await settled_io(lambda: self._save(batch, "new")))
        if not ids:
            raise ValueError("Fulltext cannot be empty")
        old = []
        old_ids = set()
        iterator = await settled_io(
            lambda: self.client.query_iterator(
                "arxiv_chunks",
                filter=f"arxiv_id == '{escape_sql(self.identity['arxiv_id'])}'",
                output_fields=_FIELDS,
                batch_size=_BATCH,
                consistency_level="Strong",
                timeout=45,
            )
        )
        try:
            while True:
                await self.guard()
                rows = await settled_io(iterator.next)
                if not rows:
                    break
                for row in rows:
                    pk = row["chunk_id"]
                    if pk in old_ids:
                        raise ValueError("Duplicate old fulltext primary key")
                    old_ids.add(pk)
                if len(old_ids) > MAX_PAPER_CHUNKS:
                    raise ValueError("Old fulltext exceeds per-paper chunk bound")
                old.append(await settled_io(partial(self._save, rows, "old")))
        finally:
            await settled_io(iterator.close)
        if ids & old_ids:
            raise ValueError("New fulltext primary keys must not overwrite the old revision")
        await self._paper()
        manifest = {
            "format": "scholight.fulltext-install.v1",
            "identity": self.identity,
            "new": new,
            "old": old,
            "paper_before": paper,
            "flags": flags,
            "chunk_count": len(ids),
            "chunks_sha256": digest_json(
                sorted((pk, sha) for shard in new for pk, sha in shard["digests"].items())
            ),
        }
        await settled_io(
            lambda: self.location.write_json("manifest.json", manifest, self.workspace)
        )
        await self.record("prepared", manifest)
        return manifest

    async def _manifest(self) -> dict[str, Any]:
        manifest = await settled_io(lambda: self.location.read_json("manifest.json"))
        if (
            manifest.get("format") != "scholight.fulltext-install.v1"
            or manifest.get("identity") != self.identity
        ):
            raise ValueError("Fulltext recovery manifest target or version mismatch")
        seen: set[str] = set()
        for key in ("old", "new"):
            for shard in manifest[key]:
                rows = await settled_io(partial(self._load, shard))
                for row in rows:
                    pk = row["chunk_id"]
                    if pk in seen:
                        raise ValueError("Duplicate fulltext recovery primary key")
                    seen.add(pk)
        pairs = sorted(
            (pk, sha) for shard in manifest["new"] for pk, sha in shard["digests"].items()
        )
        if (
            not pairs
            or len(pairs) != manifest["chunk_count"]
            or len(pairs) > MAX_PAPER_CHUNKS
            or digest_json(pairs) != manifest["chunks_sha256"]
        ):
            raise ValueError("Fulltext manifest count or checksum mismatch")
        return manifest

    async def apply(self) -> dict[str, Any]:
        await self._paper()
        manifest = await self._manifest()  # Validate every recovery shard before writes.
        await self.record("prepared", manifest)
        for shard in manifest["new"]:
            rows = await settled_io(partial(self._load, shard))
            await self._paper()
            await _settled_write(
                partial(
                    self.client.upsert,
                    "arxiv_chunks",
                    data=rows,
                    consistency_level="Strong",
                    timeout=45,
                )
            )
        await self.record("written", manifest)
        for shard in manifest["new"]:
            await self.guard()
            rows = await settled_io(
                partial(
                    self.client.get,
                    "arxiv_chunks",
                    ids=list(shard["digests"]),
                    output_fields=_FIELDS,
                    consistency_level="Strong",
                    timeout=45,
                )
            )
            if (
                len(rows) != shard["rows"]
                or {r["chunk_id"]: chunk_digest(r, self.identity["dimension"]) for r in rows}
                != shard["digests"]
            ):
                raise ValueError("Fulltext target verification failed before cleanup")
        await self.record("verified", manifest)
        for shard in manifest["old"]:
            await self._paper()
            ids = list(shard["digests"])
            await _settled_write(
                partial(
                    self.client.delete,
                    "arxiv_chunks",
                    ids=ids,
                    consistency_level="Strong",
                    timeout=45,
                )
            )
            left = await settled_io(
                partial(
                    self.client.get,
                    "arxiv_chunks",
                    ids=ids,
                    output_fields=["chunk_id"],
                    consistency_level="Strong",
                    timeout=45,
                )
            )
            if left:
                raise ValueError("Old fulltext cleanup verification failed")
        await self.record("cleaned", manifest)
        await self._paper()
        await _settled_write(
            lambda: self.client.upsert(
                "arxiv_papers",
                data=[
                    {"arxiv_id": self.identity["arxiv_id"], **manifest["flags"], "has_chunks": True}
                ],
                partial_update=True,
                consistency_level="Strong",
                timeout=45,
            )
        )
        await self.guard()
        await self.record("complete", manifest)
        return manifest
