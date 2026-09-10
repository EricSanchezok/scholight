"""Versioned, lossless collection archives with independent restoration proof.

Only this opt-in module imports Arrow. No source mutation is performed. A source
write freeze is an explicit operator assertion, not inferred from stable counts.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import math
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from scholight.config import settings
from scholight.store.archive_io import ArchiveLocation, file_digest
from scholight.store.export import _json_default

_FORMAT = "scholight-parquet-v1"
_MANIFEST = "manifest.json"
_COLLECTIONS = {"arxiv_papers": "abstract_embedding", "arxiv_chunks": "content_embedding"}
_BATCH_SIZE = 128
_SHARD_BYTES = 64 * 1024 * 1024
_SCRATCH_BYTES = 32 * 1024**3


def _workspace() -> tempfile.TemporaryDirectory[str]:
    root = Path(settings.data_root) / "archive-work"
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix="run-", dir=root)


def _endpoint(uri: str) -> str:
    parsed = urlsplit(uri)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValueError("An explicit credential-free Zilliz endpoint is required")
    if parsed.query or parsed.fragment:
        raise ValueError("Zilliz endpoint must not contain query credentials")
    return uri.rstrip("/").lower()


def _count(client: Any, collection: str) -> int:
    rows = client.query(
        collection, filter="", output_fields=["count(*)"], consistency_level="Strong"
    )
    return int(rows[0]["count(*)"])


def _schema_contract(schema: dict[str, Any]) -> dict[str, Any]:
    # Collection IDs and runtime load/index state are deployment-specific.
    fields = []
    for field in schema["fields"]:
        fields.append(
            {key: value for key, value in field.items() if key not in {"field_id", "description"}}
        )
    functions = [
        {
            key: value
            for key, value in function.items()
            if key
            not in {"id", "function_id", "description", "input_field_ids", "output_field_ids"}
        }
        for function in schema.get("functions", [])
    ]
    return {
        "fields": fields,
        "functions": functions,
        "enable_dynamic_field": schema.get("enable_dynamic_field", False),
    }


def _manifest(location: ArchiveLocation) -> dict[str, Any]:
    result = location.read_json(_MANIFEST)
    if result.get("format") != _FORMAT or result.get("collection") not in _COLLECTIONS:
        raise ValueError("Unsupported archive format or collection")
    if not isinstance(result.get("shards"), list):
        raise ValueError("Missing archive shard list")
    return result


def _fields(schema: dict[str, Any]) -> tuple[str, list[str]]:
    generated = {
        name
        for function in schema.get("functions", [])
        for name in function.get("output_field_names", [])
    }
    primary = [field["name"] for field in schema["fields"] if field.get("is_primary")]
    if len(primary) != 1:
        raise ValueError("Archive requires exactly one primary key")
    return str(primary[0]), [
        str(field["name"]) for field in schema["fields"] if field["name"] not in generated
    ]


def _dimension(schema: dict[str, Any], vector: str) -> int:
    field = next(field for field in schema["fields"] if field["name"] == vector)
    if int(field["type"]) != 101:
        raise ValueError("Archive requires FLOAT_VECTOR embeddings")
    return int(field["params"]["dim"])


def _validate_scalar(value: Any, field: dict[str, Any]) -> None:
    kind, params = int(field["type"]), field.get("params", {})
    if value is None and field.get("nullable"):
        return
    valid = False
    if kind == 1:
        valid = isinstance(value, bool)
    elif kind in {2, 3, 4, 5}:
        bits = {2: 8, 3: 16, 4: 32, 5: 64}[kind]
        valid = type(value) is int and -(2 ** (bits - 1)) <= value < 2 ** (bits - 1)
    elif kind in {10, 11}:
        valid = type(value) in {int, float} and math.isfinite(value)
    elif kind == 21:
        valid = isinstance(value, str) and len(value.encode()) <= int(params["max_length"])
    elif kind == 22:
        valid = isinstance(value, list) and len(value) <= int(params["max_capacity"])
        if valid:
            for item in value:
                _validate_scalar(item, {"type": field["element_type"], "params": params})
    elif kind == 23:
        valid = isinstance(value, (dict, list))
    elif kind == 104:
        valid = isinstance(value, dict) and all(
            str(k).isdigit() and 0 <= int(k) < 2**32 and math.isfinite(float(v))
            for k, v in value.items()
        )
    if not valid:
        raise ValueError(
            "Invalid archive scalar value for schema field " + str(field.get("name", kind))
        )


def _code_digest() -> str:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        if "tests" not in path.parts:
            digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def _encoded(row: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    row = {
        name: list(value) if type(value).__name__.startswith("Repeated") else value
        for name, value in row.items()
    }
    vector_name, pk = manifest["vector_field"], manifest["primary_key"]
    if set(row) != set(manifest["output_fields"]):
        raise ValueError("Archive row fields differ from schema")
    vector = row[vector_name]
    if len(vector) != manifest["dimension"] or not all(math.isfinite(float(v)) for v in vector):
        raise ValueError("Invalid embedding dimension or non-finite vector")
    if not isinstance(row[pk], str) or not row[pk]:
        raise ValueError("Invalid archive primary key")
    for field in manifest["schema"]["fields"]:
        if field["name"] in row and field["name"] != vector_name:
            _validate_scalar(row[field["name"]], field)
    payload = {key: value for key, value in row.items() if key != vector_name}
    return {
        "pk": row[pk],
        "vector": vector,
        "payload": json.dumps(
            payload, sort_keys=True, ensure_ascii=False, allow_nan=False
        ).encode(),
    }


def _decoded(record: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    row = json.loads(record["payload"])
    row[manifest["vector_field"]] = record["vector"]
    _encoded(row, manifest)
    if row[manifest["primary_key"]] != record["pk"]:
        raise ValueError("Archive primary key differs from payload")
    # JSON object keys representing sparse indexes are strings on disk.
    for field in manifest["schema"]["fields"]:
        if int(field["type"]) == 104 and field["name"] in row:
            row[field["name"]] = {int(k): v for k, v in row[field["name"]].items()}
    return cast("dict[str, Any]", row)


def _row_digest(row: dict[str, Any], manifest: dict[str, Any]) -> str:
    record = _encoded(row, manifest)
    vector = pa.array(record["vector"], type=pa.float32()).buffers()[1].to_pybytes()
    return hashlib.sha256(record["payload"] + b"\0" + vector).hexdigest()


def _scan(
    location: ArchiveLocation, manifest: dict[str, Any], workspace: Path
) -> Iterator[dict[str, Any]]:
    seen_names: set[str] = set()
    for shard in manifest["shards"]:
        name = shard["name"]
        if name in seen_names or not name.endswith(".parquet"):
            raise ValueError("Duplicate or invalid archive shard")
        seen_names.add(name)
        path = workspace / "read.parquet"
        location.download(name, path)
        try:
            if path.stat().st_size != shard["bytes"] or file_digest(path) != shard["sha256"]:
                raise ValueError("Archive shard checksum mismatch")
            count = 0
            with pq.ParquetFile(path) as parquet:
                for batch in parquet.iter_batches(batch_size=_BATCH_SIZE):
                    for record in batch.to_pylist():
                        count += 1
                        yield _decoded(record, manifest)
            if count != shard["rows"]:
                raise ValueError("Archive shard row count differs")
        finally:
            path.unlink(missing_ok=True)


def _validate(
    location: ArchiveLocation, manifest: dict[str, Any], workspace: Path, *, max_scratch_bytes: int
) -> int:
    path = workspace / "keys.sqlite"
    count = 0
    with closing(sqlite3.connect(path)) as keys:
        keys.execute("PRAGMA journal_mode=OFF")
        keys.execute("PRAGMA cache_size=-8192")
        keys.execute("CREATE TABLE keys (pk TEXT PRIMARY KEY) WITHOUT ROWID")
        try:
            for row in _scan(location, manifest, workspace):
                keys.execute("INSERT INTO keys VALUES (?)", (row[manifest["primary_key"]],))
                count += 1
                if count % 1000 == 0:
                    keys.commit()
                    if path.stat().st_size > max_scratch_bytes:
                        raise ValueError("Archive verification scratch limit exceeded")
        except sqlite3.IntegrityError as exc:
            raise ValueError("Duplicate primary key in archive") from exc
    path.unlink()
    if count != manifest["rows"]:
        raise ValueError("Archive total row count differs")
    if manifest["frozen"] and count != manifest["source_rows"]:
        raise ValueError("Archive does not cover the frozen source row count")
    return count


def verify_archive(
    destination: str, *, s3_client: Any = None, max_scratch_bytes: int = _SCRATCH_BYTES
) -> dict[str, Any]:
    """Read every shard and reject corruption, duplicate keys, and incomplete jobs."""
    location = ArchiveLocation(destination, s3_client=s3_client)
    manifest = _manifest(location)
    if not manifest["complete"]:
        raise ValueError("Archive is incomplete")
    with _workspace() as directory:
        count = _validate(location, manifest, Path(directory), max_scratch_bytes=max_scratch_bytes)
    return {
        "archive_id": manifest["archive_id"],
        "rows": count,
        "integrity_verified": True,
        "final_archive": manifest["frozen"],
        "restoration_verified": False,
    }


def export_archive(
    client: Any,
    collection: str,
    destination: str,
    *,
    source_uri: str,
    frozen: bool,
    shard_bytes: int = _SHARD_BYTES,
    s3_client: Any = None,
) -> dict[str, Any]:
    """Resume only committed shards; checkpoint advancement follows durable upload."""
    if collection not in _COLLECTIONS or not 1 <= shard_bytes <= 256 * 1024**2:
        raise ValueError("Invalid collection or shard byte limit")
    endpoint = _endpoint(source_uri)
    location = ArchiveLocation(destination, s3_client=s3_client)
    schema = json.loads(json.dumps(client.describe_collection(collection), default=_json_default))
    pk, output_fields = _fields(schema)
    vector = _COLLECTIONS[collection]
    with _workspace() as directory:
        workspace = Path(directory)
        if location.exists(_MANIFEST):
            manifest = _manifest(location)
            if (
                manifest["source_uri"],
                manifest["collection"],
                manifest["schema"].get("collection_id"),
                manifest["frozen"],
            ) != (endpoint, collection, schema.get("collection_id"), frozen):
                raise ValueError("Archive source identity or freeze declaration changed")
            if _schema_contract(schema) != _schema_contract(manifest["schema"]):
                raise ValueError("Archive source schema changed")
            if manifest["complete"]:
                return verify_archive(destination, s3_client=s3_client)
        else:
            indexes = [
                client.describe_index(collection, name) for name in client.list_indexes(collection)
            ]
            manifest = {
                "format": _FORMAT,
                "archive_id": uuid4().hex,
                "collection": collection,
                "source_uri": endpoint,
                "schema": schema,
                "indexes": indexes,
                "primary_key": pk,
                "output_fields": output_fields,
                "vector_field": vector,
                "dimension": _dimension(schema, vector),
                "model": settings.embedding_model,
                "code_sha256": _code_digest(),
                "versions": {
                    name: importlib.metadata.version(name)
                    for name in (
                        "scholight",
                        "pymilvus",
                        "pyarrow",
                        "boto3",
                        "botocore",
                        "numpy",
                        "protobuf",
                    )
                },
                "frozen": frozen,
                "source_rows": _count(client, collection),
                "rows": 0,
                "shards": [],
                "checkpoint": None,
                "complete": False,
            }
            location.write_json(_MANIFEST, manifest, workspace)
        checkpoint = workspace / "iterator.checkpoint"
        if manifest["checkpoint"]:
            checkpoint.write_bytes(base64.b64decode(manifest["checkpoint"], validate=True))
        iterator = client.query_iterator(
            collection,
            batch_size=_BATCH_SIZE,
            filter="",
            output_fields=output_fields,
            iterator_cp_file=str(checkpoint),
            timeout=60,
        )
        arrow_schema = pa.schema(
            [
                ("pk", pa.string()),
                ("vector", pa.list_(pa.float32(), manifest["dimension"])),
                ("payload", pa.binary()),
            ]
        )
        writer: pq.ParquetWriter | None = None
        shard_path = workspace / "write.parquet"
        pending_rows = pending_bytes = 0

        def commit_shard() -> None:
            nonlocal writer, pending_rows, pending_bytes
            if writer is None:
                return
            writer.close()
            writer = None
            name = f"shard-{uuid4().hex}.parquet"
            location.upload(shard_path, name)
            check = workspace / "uploaded.parquet"
            location.download(name, check)
            digest = file_digest(shard_path)
            if file_digest(check) != digest:
                raise ValueError("Uploaded shard checksum mismatch")
            check.unlink()
            manifest["shards"].append(
                {
                    "name": name,
                    "rows": pending_rows,
                    "bytes": shard_path.stat().st_size,
                    "sha256": digest,
                }
            )
            manifest["rows"] += pending_rows
            manifest["checkpoint"] = base64.b64encode(checkpoint.read_bytes()).decode()
            location.write_json(_MANIFEST, manifest, workspace)
            shard_path.unlink()
            pending_rows = pending_bytes = 0

        try:
            while rows := iterator.next():
                table = pa.Table.from_pylist(
                    [_encoded(row, manifest) for row in rows], schema=arrow_schema
                )
                if writer is None:
                    writer = pq.ParquetWriter(shard_path, arrow_schema, compression="zstd")
                writer.write_table(table)
                pending_rows += len(rows)
                pending_bytes += table.nbytes
                if pending_bytes >= shard_bytes:
                    commit_shard()
            commit_shard()
        finally:
            if writer is not None:
                writer.close()
            iterator.close()
        if frozen and _count(client, collection) != manifest["source_rows"]:
            raise ValueError("Frozen source row count changed during export")
        _validate(location, manifest, workspace, max_scratch_bytes=_SCRATCH_BYTES)
        manifest["complete"] = True
        location.write_json(_MANIFEST, manifest, workspace)
    return {
        "archive_id": manifest["archive_id"],
        "rows": manifest["rows"],
        "integrity_verified": True,
        "final_archive": frozen,
        "restoration_verified": False,
    }


def _target(
    client: Any, collection: str, manifest: dict[str, Any], uri: str
) -> tuple[str, dict[str, Any]]:
    endpoint = _endpoint(uri)
    if collection != manifest["collection"]:
        raise ValueError("Selected collection differs from archive")
    if endpoint == manifest["source_uri"]:
        raise ValueError("Refusing to restore into the source endpoint")
    schema = json.loads(json.dumps(client.describe_collection(collection), default=_json_default))
    if schema.get("collection_id") == manifest["schema"].get("collection_id"):
        raise ValueError("Target collection identity matches the source")
    if _schema_contract(schema) != _schema_contract(manifest["schema"]):
        raise ValueError("Target schema differs from archive; initialize it explicitly first")
    identity = {
        "uri": endpoint,
        "collection": collection,
        "collection_id": schema.get("collection_id"),
        "archive_id": manifest["archive_id"],
    }
    name = (
        "restore-"
        + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        + ".json"
    )
    return name, identity


def initialize_archive_target(
    client: Any, collection: str, destination: str, *, target_uri: str, s3_client: Any = None
) -> None:
    """Explicitly initialize only the selected empty target, without replacing data."""
    from pymilvus import CollectionSchema

    verify_archive(destination, s3_client=s3_client)
    manifest = _manifest(ArchiveLocation(destination, s3_client=s3_client))
    if collection != manifest["collection"] or _endpoint(target_uri) == manifest["source_uri"]:
        raise ValueError(
            "Selected target matches the source or differs from the archive collection"
        )
    exists = client.has_collection(collection)
    if not exists:
        schema = CollectionSchema.construct_from_dict(manifest["schema"])
        client.create_collection(collection, schema=schema, consistency_level="Strong")
    _target(client, collection, manifest, target_uri)
    existing = set(client.list_indexes(collection))
    for index in manifest["indexes"]:
        if index["index_name"] in existing:
            continue
        params = client.prepare_index_params()
        arguments = {
            key: index[key]
            for key in ("field_name", "index_name", "index_type", "metric_type")
            if key in index
        }
        runtime_fields = {"state", "total_rows", "indexed_rows", "pending_index_rows"}
        arguments["params"] = {
            key: value
            for key, value in index.items()
            if key not in arguments and key not in runtime_fields
        }
        params.add_index(**arguments)
        client.create_index(collection, params)
    client.load_collection(collection, timeout=3600)
    if _count(client, collection):
        raise ValueError("Archive initialization requires an empty target")


def restore_archive(
    client: Any, collection: str, destination: str, *, target_uri: str, s3_client: Any = None
) -> dict[str, Any]:
    """Validate everything before writing. An existing matching job may resume."""
    verify_archive(destination, s3_client=s3_client)
    location = ArchiveLocation(destination, s3_client=s3_client)
    manifest = _manifest(location)
    state_name, identity = _target(client, collection, manifest, target_uri)
    with _workspace() as directory:
        workspace = Path(directory)
        if location.exists(state_name):
            state = location.read_json(state_name)
            if state["identity"] != identity:
                raise ValueError("Restore job identity differs")
        else:
            if _count(client, collection) != 0:
                raise ValueError("Restore requires an empty target collection")
            state = {"identity": identity, "shards_done": 0, "complete": False}
            location.write_json(state_name, state, workspace)
        for index, shard in enumerate(manifest["shards"]):
            if index < state["shards_done"]:
                continue
            one = {**manifest, "shards": [shard]}
            batch: list[dict[str, Any]] = []
            for row in _scan(location, one, workspace):
                batch.append(row)
                if len(batch) == _BATCH_SIZE:
                    client.upsert(collection, data=batch, consistency_level="Strong")
                    batch.clear()
            if batch:
                client.upsert(collection, data=batch, consistency_level="Strong")
            state["shards_done"] = index + 1
            location.write_json(state_name, state, workspace)
        result = verify_restored(
            client, collection, destination, target_uri=target_uri, s3_client=s3_client
        )
        state["complete"] = True
        state["verification"] = result
        location.write_json(state_name, state, workspace)
    return result


def verify_restored(
    client: Any, collection: str, destination: str, *, target_uri: str, s3_client: Any = None
) -> dict[str, Any]:
    """Compare every restored scalar and vector by primary key, in bounded memory."""
    result = verify_archive(destination, s3_client=s3_client)
    location = ArchiveLocation(destination, s3_client=s3_client)
    manifest = _manifest(location)
    _target(client, collection, manifest, target_uri)
    if _count(client, collection) != manifest["rows"]:
        raise ValueError("Target row count differs from archive")
    with _workspace() as directory:
        workspace = Path(directory)
        with closing(sqlite3.connect(workspace / "proof.sqlite")) as proof:
            proof.execute("PRAGMA journal_mode=OFF")
            proof.execute("PRAGMA cache_size=-8192")
            proof.execute(
                "CREATE TABLE proof (pk TEXT PRIMARY KEY, digest TEXT, seen INTEGER DEFAULT 0) WITHOUT ROWID"
            )
            for proof_count, row in enumerate(_scan(location, manifest, workspace), start=1):
                if proof_count % 1000 == 0:
                    proof.commit()
                    if (workspace / "proof.sqlite").stat().st_size > _SCRATCH_BYTES:
                        raise ValueError("Restore verification scratch limit exceeded")
                proof.execute(
                    "INSERT INTO proof(pk,digest) VALUES (?,?)",
                    (row[manifest["primary_key"]], _row_digest(row, manifest)),
                )
            proof.commit()
            iterator = client.query_iterator(
                collection,
                batch_size=_BATCH_SIZE,
                filter="",
                output_fields=manifest["output_fields"],
                iterator_cp_file=str(workspace / "target.checkpoint"),
                timeout=60,
            )
            try:
                while rows := iterator.next():
                    for row in rows:
                        changed = proof.execute(
                            "UPDATE proof SET seen=1 WHERE pk=? AND digest=? AND seen=0",
                            (row[manifest["primary_key"]], _row_digest(row, manifest)),
                        ).rowcount
                        if changed != 1:
                            raise ValueError("Restored row differs, is unexpected, or repeats")
                    proof.commit()
                if proof.execute("SELECT count(*) FROM proof WHERE seen=0").fetchone()[0]:
                    raise ValueError("Restored target is missing archive rows")
            finally:
                iterator.close()
    return {**result, "restoration_verified": True, "verified_rows": manifest["rows"]}
