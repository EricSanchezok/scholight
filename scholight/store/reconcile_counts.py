"""Explicit physical-count evidence for otherwise unique reconciliation inventories."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from scholight.models.ingestion_target import digest_json

FIELDS = ["arxiv_id", "version", "updated", "created"]


def checked_proofs(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    proofs = manifest.get("count_duplicates", [])
    if not isinstance(proofs, list) or len(proofs) > 128:
        raise ValueError("Invalid duplicate count evidence")
    seen = set()
    for proof in proofs:
        if (
            not isinstance(proof, dict)
            or set(proof) != {"arxiv_id", "physical_count", "scalar_sha256"}
            or not isinstance(proof["arxiv_id"], str)
            or not 1 <= len(proof["arxiv_id"]) <= 32
            or proof["arxiv_id"] in seen
            or type(proof["physical_count"]) is not int
            or proof["physical_count"] < 2
            or not isinstance(proof["scalar_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", proof["scalar_sha256"])
        ):
            raise ValueError("Invalid duplicate count evidence")
        seen.add(proof["arxiv_id"])
    return list(proofs)


def check_total(manifest: dict[str, Any]) -> None:
    excess = sum(p["physical_count"] - 1 for p in checked_proofs(manifest))
    if manifest["rows"] + excess != manifest["expected_rows"]:
        raise ValueError("Unexplained inventory count difference")


def check_inventory_proofs(conn: sqlite3.Connection, table: str, manifest: dict[str, Any]) -> None:
    if table not in {"source", "target"}:
        raise ValueError("Unexpected inventory table")
    for proof in checked_proofs(manifest):
        row = conn.execute(
            "SELECT * FROM source WHERE pk=?"
            if table == "source"
            else "SELECT * FROM target WHERE pk=?",
            (proof["arxiv_id"],),
        ).fetchone()
        if (
            row is None
            or digest_json(dict(zip(FIELDS, row, strict=True))) != proof["scalar_sha256"]
        ):
            raise ValueError("Duplicate count evidence does not match the inventory record")


def prove_counts(
    client: Any, conn: sqlite3.Connection, ids: tuple[str, ...]
) -> list[dict[str, Any]]:
    if len(ids) > 128 or len(set(ids)) != len(ids):
        raise ValueError("Explicit duplicate count IDs must be unique and bounded")
    proofs = []
    for pk in sorted(ids):
        if not 1 <= len(pk) <= 32:
            raise ValueError("Invalid duplicate count ID")
        stored = conn.execute("SELECT * FROM source WHERE pk=?", (pk,)).fetchone()
        if stored is None:
            raise ValueError("A duplicate count ID is missing from the inventory")
        visible = client.get(
            "arxiv_papers", ids=[pk], output_fields=FIELDS, consistency_level="Strong", timeout=60
        )
        if len(visible) != 1 or dict(visible[0]) != dict(zip(FIELDS, stored, strict=True)):
            raise ValueError("Visible duplicate count record changed after scanning")
        count = client.query(
            "arxiv_papers",
            filter="arxiv_id == " + json.dumps(pk),
            output_fields=["count(*)"],
            consistency_level="Strong",
            timeout=60,
        )[0]["count(*)"]
        if type(count) is not int or count < 2:
            raise ValueError("No repeated physical count for the declared duplicate ID")
        proofs.append(
            {
                "arxiv_id": pk,
                "physical_count": count,
                "scalar_sha256": digest_json(dict(visible[0])),
            }
        )
    return proofs
