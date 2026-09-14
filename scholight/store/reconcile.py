"""Apply and verify version-aware abstract deltas with durable before-images."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pyarrow as pa

from scholight.models.ingestion_target import digest_json
from scholight.store.archive_io import ArchiveLocation
from scholight.store.fields import PAPER_ALL_FIELDS
from scholight.store.reconcile_inventory import (
    build_delta,
    persist_rows,
    read_rows,
    scan_inventory,
    validate_delta,
)

_FLAGS = {"has_chunks", "has_latex", "has_pdf", "has_markdown"}
_FIELDS = [name for name in PAPER_ALL_FIELDS if name != "abstract_bm25"]


def merge_paper(source: dict[str, Any], before: dict[str, Any] | None) -> dict[str, Any] | None:
    """Source metadata never overrides destination fulltext state or a newer version."""
    if before and (
        source["version"] < before["version"]
        or (source["version"] == before["version"] and source["updated"] < before["updated"])
    ):
        return None
    result = dict(source)
    for flag in _FLAGS:
        result[flag] = before.get(flag, False) if before else False
    return result


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    return {
        name: list(value) if type(value).__name__.startswith("Repeated") else value
        for name, value in row.items()
    }


def _digest(row: dict[str, Any], dimension: int) -> str:
    row = _normalize(row)
    vector = row["abstract_embedding"]
    if len(vector) != dimension or not all(math.isfinite(float(v)) for v in vector):
        raise ValueError("Unexpected abstract vector dimension")
    raw = json.dumps(
        {k: v for k, v in row.items() if k != "abstract_embedding"},
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    values = pa.array(vector, type=pa.float32()).buffers()[1].to_pybytes()
    return hashlib.sha256(raw + b"\0" + values).hexdigest()


class AbstractReconciliation:
    def __init__(
        self,
        source: Any,
        target: Any,
        plan_uri: str,
        *,
        workspace: Path,
        dimension: int,
        model: str,
    ) -> None:
        self.source = source
        self.target = target
        self.location = ArchiveLocation(plan_uri)
        self.uri = plan_uri
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.dimension = dimension
        self.model = model
        self.plan = self.location.read_json("manifest.json")
        self.plan_sha = digest_json(self.plan)

    def _guard(self) -> None:
        if self.plan["binding"]["source"] == self.plan["binding"]["target"]:
            raise ValueError("Source and target must differ for abstract writes")
        for name, client in [("source", self.source), ("target", self.target)]:
            actual = str(client.describe_collection("arxiv_papers")["collection_id"])
            if actual != self.plan["binding"][name]["collection_id"]:
                raise ValueError("Actual reconciliation collection identity changed")
        if digest_json(self.location.read_json("manifest.json")) != self.plan_sha:
            raise ValueError("Reconciliation plan changed")

    def _get(self, client: Any, ids: list[str]) -> dict[str, dict[str, Any]]:
        rows = client.get(
            "arxiv_papers", ids=ids, output_fields=_FIELDS, consistency_level="Strong", timeout=60
        )
        keyed = {row["arxiv_id"]: _normalize(row) for row in rows}
        if len(keyed) != len(rows) or not set(keyed) <= set(ids):
            raise ValueError("Duplicate or unexpected reconciliation primary key")
        return keyed

    def _save(self, rows: list[dict[str, Any]], work: Path, prefix: str) -> dict[str, Any]:
        records = []
        hashes = {}
        for raw in rows:
            row = _normalize(raw)
            pk = row["arxiv_id"]
            hashes[pk] = _digest(row, self.dimension)
            records.append(
                {
                    "pk": pk,
                    "vector": row["abstract_embedding"],
                    "payload": json.dumps(
                        {k: v for k, v in row.items() if k != "abstract_embedding"},
                        sort_keys=True,
                        ensure_ascii=False,
                        allow_nan=False,
                    ).encode(),
                }
            )
        schema = pa.schema(
            [
                ("pk", pa.string()),
                ("vector", pa.list_(pa.float32(), self.dimension)),
                ("payload", pa.binary()),
            ]
        )
        return {
            **persist_rows(self.location, records, work, prefix=prefix, schema=schema),
            "digests": hashes,
        }

    def _load(self, shard: dict[str, Any], work: Path) -> dict[str, dict[str, Any]]:
        rows = {}
        for item in read_rows(self.location, [shard], work):
            row = json.loads(item["payload"])
            row["abstract_embedding"] = item["vector"]
            if row["arxiv_id"] != item["pk"] or item["pk"] in rows:
                raise ValueError("Recovery primary key mismatch")
            rows[item["pk"]] = row
        if {pk: _digest(row, self.dimension) for pk, row in rows.items()} != shard["digests"]:
            raise ValueError("Recovery row checksum mismatch")
        return rows

    def _candidates(self, work: Path) -> Iterator[list[dict[str, Any]]]:
        batch = []
        for row in read_rows(self.location, self.plan["shards"], work):
            batch.append(row)
            if len(batch) == 64:
                yield batch
                batch = []
        if batch:
            yield batch

    def _prepare(self, index: int, candidates: list[dict[str, Any]], work: Path) -> dict[str, Any]:
        name = f"batch-{index:06}.json"
        if self.location.exists(name):
            batch = self.location.read_json(name)
            if batch.get("plan_sha256") != self.plan_sha or batch.get(
                "candidates_sha256"
            ) != digest_json(candidates):
                raise ValueError("Prepared reconciliation batch differs from plan")
            return batch
        ids = [row["arxiv_id"] for row in candidates]
        sources = self._get(self.source, ids)
        targets = self._get(self.target, ids)
        desired = []
        for entry in candidates:
            pk = entry["arxiv_id"]
            source = sources.get(pk)
            before = targets.get(pk)
            if source is None or (source["version"], source["updated"]) != (
                entry["source_version"],
                entry["source_updated"],
            ):
                raise ValueError("Frozen source changed since planning")
            if ((before["version"], before["updated"]) if before else (None, None)) != (
                entry["target_version"],
                entry["target_updated"],
            ):
                raise ValueError("Frozen target changed since planning; no overwrite attempted")
            merged = merge_paper(source, before)
            if merged is None:
                raise ValueError("Target advanced beyond prepared candidate")
            desired.append(merged)
        batch = {
            "plan_sha256": self.plan_sha,
            "candidates_sha256": digest_json(candidates),
            "dimension": self.dimension,
            "model": self.model,
            "before": self._save(list(targets.values()), work, "before"),
            "after": self._save(desired, work, "after"),
        }
        self.location.write_json(name, batch, work)
        return batch

    def _verify_batch(self, batch: dict[str, Any], work: Path) -> None:
        expected = self._load(batch["after"], work)
        actual = self._get(self.target, list(expected))
        if len(actual) != len(expected) or any(
            _digest(actual[pk], self.dimension) != _digest(row, self.dimension)
            for pk, row in expected.items()
            if pk in actual
        ):
            raise ValueError("Abstract target verification failed")

    def apply(self) -> dict[str, Any]:
        self._guard()
        with TemporaryDirectory(prefix="abstract-apply-", dir=self.workspace) as directory:
            work = Path(directory)
            validate_delta(self.location, self.plan, work)
            if self.location.exists("apply.json"):
                state = self.location.read_json("apply.json")
                if (
                    state.get("plan_sha256") != self.plan_sha
                    or state.get("model") != self.model
                    or state.get("dimension") != self.dimension
                ):
                    raise ValueError("Reconciliation apply configuration changed")
            else:
                state = {
                    "plan_sha256": self.plan_sha,
                    "model": self.model,
                    "dimension": self.dimension,
                    "batches": [],
                    "verified_candidates": 0,
                    "complete": False,
                }
                self.location.write_json("apply.json", state, work)
            for index, candidates in enumerate(self._candidates(work)):
                self._guard()
                batch = self._prepare(index, candidates, work)
                if batch["dimension"] != self.dimension or batch["model"] != self.model:
                    raise ValueError("Prepared abstract model or dimension changed")
                before = self._load(batch["before"], work)
                desired = self._load(batch["after"], work)
                if index < len(state["batches"]):
                    if state["batches"][index] != digest_json(batch):
                        raise ValueError("Committed batch checksum changed")
                    self._verify_batch(batch, work)
                    continue
                current = self._get(self.target, list(desired))
                # A replay may see either the saved before-image or the already written after-image.
                for pk, row in desired.items():
                    actual = current.get(pk)
                    valid = {_digest(row, self.dimension)}
                    if pk in before:
                        valid.add(_digest(before[pk], self.dimension))
                    if (actual is None and pk in before) or (
                        actual is not None and _digest(actual, self.dimension) not in valid
                    ):
                        raise ValueError("Target changed outside the prepared reconciliation batch")
                inserts = [row for pk, row in desired.items() if pk not in before]
                updates = [
                    {k: v for k, v in row.items() if k not in _FLAGS}
                    for pk, row in desired.items()
                    if pk in before
                ]
                if inserts:
                    self.target.upsert(
                        "arxiv_papers", data=inserts, consistency_level="Strong", timeout=60
                    )
                if updates:
                    self.target.upsert(
                        "arxiv_papers",
                        data=updates,
                        partial_update=True,
                        consistency_level="Strong",
                        timeout=60,
                    )
                self._verify_batch(batch, work)
                state["batches"].append(digest_json(batch))
                state["verified_candidates"] += len(candidates)
                self.location.write_json("apply.json", state, work)
            if state["verified_candidates"] != self.plan["candidates"]:
                raise ValueError("Reconciliation committed count differs from plan")
            state["complete"] = True
            self.location.write_json("apply.json", state, work)
        return state

    def verify(self, *, frozen: bool) -> dict[str, Any]:
        if not frozen:
            raise ValueError("Target writers must remain stopped until verification finishes")
        self._guard()
        with TemporaryDirectory(prefix="abstract-verify-", dir=self.workspace) as directory:
            work = Path(directory)
            state = self.location.read_json("apply.json")
            if not state.get("complete") or state.get("plan_sha256") != self.plan_sha:
                raise ValueError("Abstract apply is incomplete or belongs to a different plan")
            validate_delta(self.location, self.plan, work)
            verified = 0
            for index, candidates in enumerate(self._candidates(work)):
                batch = self._prepare(index, candidates, work)
                if index >= len(state["batches"]) or state["batches"][index] != digest_json(batch):
                    raise ValueError("Reconciliation batch proof is missing or changed")
                self._load(batch["before"], work)
                self._verify_batch(batch, work)
                verified += len(candidates)
            if (
                verified != self.plan["candidates"]
                or len(state["batches"]) != (verified + 63) // 64
            ):
                raise ValueError("Full abstract verification count mismatch")
            target_uri = self.uri.rstrip("/") + "/verified-target"
            original = ArchiveLocation(self.plan["target_inventory"]).read_json("manifest.json")
            final = scan_inventory(
                self.target,
                target_uri,
                expected_uri=self.plan["binding"]["target"]["uri"],
                expected_id=self.plan["binding"]["target"]["collection_id"],
                frozen=True,
                workspace=work,
                count_duplicate_ids=tuple(
                    p["arxiv_id"] for p in original.get("count_duplicates", [])
                ),
            )
            checks = {}
            for label, initial in [
                ("source", self.plan["source_inventory"]),
                ("original_target", self.plan["target_inventory"]),
            ]:
                delta = build_delta(
                    initial,
                    target_uri,
                    self.uri.rstrip("/") + "/verify-" + label,
                    workspace=work,
                    allow_same_identity=label == "original_target",
                )
                if delta["candidates"]:
                    raise ValueError("Final abstract inventory has missing or downgraded papers")
                checks[label] = digest_json(delta)
            if final["rows"] != original["rows"] + self.plan["counts"]["insert"]:
                raise ValueError("Target total changed outside the reconciliation")
            result = {
                "format": "scholight.abstract-verification.v1",
                "plan_sha256": self.plan_sha,
                "verified_candidates": verified,
                "target_rows": final["rows"],
                "target_inventory": target_uri,
                "target_manifest_sha256": digest_json(final),
                "coverage_checks": checks,
                "model": self.model,
                "dimension": self.dimension,
                "binding": self.plan["binding"],
                "complete": True,
            }
            if self.location.exists("verification.json"):
                previous = self.location.read_json("verification.json")
                if previous != result:
                    raise ValueError("Existing abstract verification proof changed")
            self.location.write_json("verification.json", result, work)
        return result
