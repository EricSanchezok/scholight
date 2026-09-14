"""Resumable scalar inventories and version-aware abstract delta planning."""

from __future__ import annotations

import base64
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from scholight.models.ingestion_target import digest_json
from scholight.store.archive_io import ArchiveLocation, file_digest
from scholight.store.reconcile_counts import (
    check_inventory_proofs,
    check_total,
    checked_proofs,
    prove_counts,
)

_FIELDS = ["arxiv_id", "version", "updated", "created"]
_SHARD_ROWS = 16_384
_SCHEMA = pa.schema(
    [
        ("arxiv_id", pa.string()),
        ("version", pa.int64()),
        ("updated", pa.string()),
        ("created", pa.string()),
    ]
)


def persist_rows(
    location: ArchiveLocation,
    rows: list[dict[str, Any]],
    workspace: Path,
    *,
    prefix: str,
    schema: Any = None,
) -> dict[str, Any]:
    path = workspace / "write.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path, compression="zstd")
    name = prefix + "-" + uuid4().hex + ".parquet"
    digest = file_digest(path)
    location.upload(path, name)
    location.download(name, path)
    if file_digest(path) != digest:
        raise ValueError("Uploaded inventory checksum mismatch")
    size = path.stat().st_size
    path.unlink()
    return {"name": name, "sha256": digest, "rows": len(rows), "bytes": size}


def read_rows(
    location: ArchiveLocation, shards: list[dict[str, Any]], workspace: Path
) -> Iterator[dict[str, Any]]:
    names = set()
    for shard in shards:
        if shard["name"] in names:
            raise ValueError("Duplicate inventory shard")
        names.add(shard["name"])
        path = workspace / "read.parquet"
        location.download(shard["name"], path)
        if path.stat().st_size != shard["bytes"] or file_digest(path) != shard["sha256"]:
            raise ValueError("Inventory shard checksum mismatch")
        count = 0
        try:
            with pq.ParquetFile(path) as table:
                for batch in table.iter_batches(batch_size=1024):
                    for row in batch.to_pylist():
                        count += 1
                        yield row
            if count != shard["rows"]:
                raise ValueError("Inventory shard count mismatch")
        finally:
            path.unlink(missing_ok=True)


def _load_inventory(location: ArchiveLocation) -> dict[str, Any]:
    manifest = location.read_json("manifest.json")
    if (
        manifest.get("format") != "scholight.paper-inventory.v1"
        or not manifest.get("complete")
        or not manifest.get("frozen")
    ):
        raise ValueError("Incomplete or non-final paper inventory")
    check_total(manifest)
    return manifest


def _table(
    conn: sqlite3.Connection,
    name: str,
    location: ArchiveLocation,
    manifest: dict[str, Any],
    workspace: Path,
) -> None:
    if name not in {"source", "target"}:
        raise ValueError("Invalid inventory table")
    conn.execute(
        f"CREATE TABLE {name}(pk TEXT PRIMARY KEY,version INTEGER NOT NULL,updated TEXT NOT NULL,created TEXT NOT NULL) WITHOUT ROWID"
    )
    count = 0
    try:
        with conn:
            for row in read_rows(location, manifest["shards"], workspace):
                pk = row["arxiv_id"]
                if (
                    not isinstance(pk, str)
                    or not 1 <= len(pk) <= 32
                    or type(row["version"]) is not int
                    or row["version"] < 1
                ):
                    raise ValueError("Unverifiable paper identifier or version in inventory")
                conn.execute(
                    (
                        "INSERT INTO source VALUES(?,?,?,?)"
                        if name == "source"
                        else "INSERT INTO target VALUES(?,?,?,?)"
                    ),
                    (pk, row["version"], row["updated"], row["created"]),
                )
                count += 1
    except sqlite3.IntegrityError as exc:
        raise ValueError("Duplicate paper primary key in inventory") from exc
    if count != manifest["rows"]:
        raise ValueError("Inventory total differs from its manifest")
    check_inventory_proofs(conn, name, manifest)


def scan_inventory(
    client: Any,
    destination: str,
    *,
    expected_uri: str,
    expected_id: str,
    frozen: bool,
    workspace: Path,
    count_duplicate_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Writers must remain stopped until reconciliation verifies and the baseline adopts."""
    if not frozen:
        raise ValueError("Source and target writers must be stopped for final reconciliation")
    identity = {
        "uri": expected_uri.rstrip("/"),
        "collection_id": str(client.describe_collection("arxiv_papers")["collection_id"]),
    }
    if identity["collection_id"] != expected_id:
        raise ValueError("Actual collection ID differs from expected target")
    workspace.mkdir(parents=True, exist_ok=True)
    location = ArchiveLocation(destination)
    with TemporaryDirectory(prefix="inventory-", dir=workspace) as directory:
        work = Path(directory)

        def count() -> int:
            return int(
                client.query(
                    "arxiv_papers",
                    filter="",
                    output_fields=["count(*)"],
                    consistency_level="Strong",
                    timeout=60,
                )[0]["count(*)"]
            )

        if location.exists("manifest.json"):
            manifest = location.read_json("manifest.json")
            if (
                manifest.get("format") != "scholight.paper-inventory.v1"
                or manifest.get("identity") != identity
                or manifest.get("frozen") != frozen
            ):
                raise ValueError("Inventory identity changed during resume")
            if manifest.get("count_duplicates") and sorted(count_duplicate_ids) != sorted(
                p["arxiv_id"] for p in checked_proofs(manifest)
            ):
                raise ValueError("Declared duplicate count IDs changed during resume")
        else:
            manifest = {
                "format": "scholight.paper-inventory.v1",
                "identity": identity,
                "frozen": frozen,
                "expected_rows": count(),
                "rows": 0,
                "checkpoint": None,
                "shards": [],
                "complete": False,
            }
            location.write_json("manifest.json", manifest, work)
        if not manifest["complete"]:
            checkpoint = work / "iterator.checkpoint"
            if manifest["checkpoint"]:
                checkpoint.write_bytes(base64.b64decode(manifest["checkpoint"], validate=True))
            iterator = client.query_iterator(
                "arxiv_papers",
                filter="",
                output_fields=_FIELDS,
                batch_size=1024,
                iterator_cp_file=str(checkpoint),
                consistency_level="Strong",
                timeout=60,
            )

            def commit(rows: list[dict[str, Any]], cursor: bytes) -> None:
                shard = persist_rows(location, rows, work, prefix="inventory", schema=_SCHEMA)
                manifest["shards"].append(shard)
                manifest["rows"] += len(rows)
                manifest["checkpoint"] = base64.b64encode(cursor).decode()
                location.write_json("manifest.json", manifest, work)

            try:
                pending: list[dict[str, Any]] = []
                cursor = b""
                while rows := iterator.next():
                    pending.extend(rows)
                    cursor = checkpoint.read_bytes()
                    if len(pending) >= _SHARD_ROWS:
                        commit(pending, cursor)
                        pending = []
                if pending:
                    commit(pending, cursor)
            finally:
                iterator.close()
        if count() != manifest["expected_rows"]:
            raise ValueError("Frozen inventory row count changed or scan was incomplete")
        with closing(sqlite3.connect(work / "inventory.sqlite")) as conn:
            conn.execute("PRAGMA cache_size=-8192")
            _table(conn, "source", location, manifest, work)
            proofs = prove_counts(client, conn, count_duplicate_ids)
            if manifest.get("count_duplicates") and manifest["count_duplicates"] != proofs:
                raise ValueError("Duplicate count evidence changed during resume")
            candidate = dict(manifest)
            if proofs:
                candidate["count_duplicates"] = proofs
            check_total(candidate)
            manifest = candidate
        manifest["complete"] = True
        location.write_json("manifest.json", manifest, work)
    return manifest


def build_delta(
    source_uri: str,
    target_uri: str,
    destination: str,
    *,
    workspace: Path,
    allow_same_identity: bool = False,
) -> dict[str, Any]:
    """Compare complete scalar scans; read full vectors only in the later apply stage."""
    source, target = ArchiveLocation(source_uri), ArchiveLocation(target_uri)
    left, right = _load_inventory(source), _load_inventory(target)
    protected = {p["arxiv_id"] for value in (left, right) for p in checked_proofs(value)}
    if left["identity"] == right["identity"] and not allow_same_identity:
        raise ValueError("Source and destination must differ")
    output = ArchiveLocation(destination)
    binding = {
        "source": left["identity"],
        "target": right["identity"],
        "source_manifest_sha256": digest_json(left),
        "target_manifest_sha256": digest_json(right),
    }
    if output.exists("manifest.json"):
        existing = output.read_json("manifest.json")
        if existing.get("binding") != binding:
            raise ValueError("Delta plan inputs changed")
        if existing.get("complete"):
            workspace.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(prefix="verify-delta-", dir=workspace) as directory:
                validate_delta(output, existing, Path(directory))
            return existing
        # An unpublished plan may be regenerated; a completed one is immutable.
    workspace.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="delta-", dir=workspace) as directory:
        work = Path(directory)
        with closing(sqlite3.connect(work / "delta.sqlite")) as conn:
            conn.execute("PRAGMA cache_size=-8192")
            _table(conn, "source", source, left, work)
            _table(conn, "target", target, right, work)
            query = """SELECT s.pk AS arxiv_id,s.version AS source_version,s.updated AS source_updated,
                t.version AS target_version,t.updated AS target_updated,
                CASE WHEN t.pk IS NULL THEN 'insert'
                    WHEN s.version>t.version OR (s.version=t.version AND s.updated>t.updated) THEN 'update'
                    WHEN s.version<t.version OR s.updated<t.updated THEN 'target_newer'
                    ELSE 'unchanged' END AS action
                FROM source s LEFT JOIN target t ON s.pk=t.pk ORDER BY s.pk"""
            conn.row_factory = sqlite3.Row
            counts = {"insert": 0, "update": 0, "target_newer": 0, "unchanged": 0}
            shards = []
            batch = []
            for row in conn.execute(query):
                counts[row["action"]] += 1
                if row["action"] in {"insert", "update"}:
                    if row["arxiv_id"] in protected:
                        raise ValueError(
                            "Ambiguous duplicate IDs cannot receive automatic delta writes"
                        )
                    batch.append(dict(row))
                    if len(batch) == 1024:
                        shards.append(persist_rows(output, batch, work, prefix="delta"))
                        batch = []
            if batch:
                shards.append(persist_rows(output, batch, work, prefix="delta"))
            manifest = {
                "format": "scholight.abstract-delta.v1",
                "binding": binding,
                "source_inventory": source_uri,
                "target_inventory": target_uri,
                "counts": counts,
                "candidates": counts["insert"] + counts["update"],
                "shards": shards,
                "complete": True,
            }
            if protected:
                manifest["protected_duplicate_ids"] = sorted(protected)
            output.write_json("manifest.json", manifest, work)
    return manifest


def validate_delta(location: ArchiveLocation, manifest: dict[str, Any], workspace: Path) -> None:
    if manifest.get("format") != "scholight.abstract-delta.v1" or not manifest.get("complete"):
        raise ValueError("Incomplete abstract delta plan")
    count = 0
    seen: set[str] = set()
    for row in read_rows(location, manifest["shards"], workspace):
        pk = row["arxiv_id"]
        if pk in seen or row["action"] not in {"insert", "update"}:
            raise ValueError("Duplicate or invalid delta candidate")
        if pk in manifest.get("protected_duplicate_ids", []):
            raise ValueError("A protected duplicate ID cannot be a delta candidate")
        seen.add(pk)
        if row["source_version"] < 1:
            raise ValueError("Unknown delta paper version")
        count += 1
    if count != manifest["candidates"]:
        raise ValueError("Delta candidate count mismatch")
