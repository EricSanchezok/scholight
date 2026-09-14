"""Explicit expand/adopt contracts; no arbitrary checksum or Identity exceptions."""

from __future__ import annotations

import hashlib
import json

# 016 only adds destination-owned tables. Legacy queue, cursor and product reads
# remain unchanged. Retire this N-1 edge after all retained rollback images adopt 016.
REVIEWED_EXPANSIONS = {
    "migrations/016_target_ingestion.sql": "7773cf744f66cbd8fd909f1b71d269bb5b963272a0ea37d748c22eb685a4aa37",
}


def contract(value: dict) -> dict:
    return {name: value[name] for name in ("identity_revision", "migrations")}


def expansion_required(running: dict, candidate: dict) -> bool:
    if running["identity_revision"] != candidate["identity_revision"]:
        raise ValueError("Identity revision changed; no reviewed compatibility path")
    old, new = running["migrations"], candidate["migrations"]
    if any(old[name] != new[name] for name in old.keys() & new.keys()):
        raise ValueError("Applied product migration checksum changed")
    added, removed = new.keys() - old.keys(), old.keys() - new.keys()
    if added and removed:
        raise ValueError("Migrations must be an append-only expansion")
    for name in added | removed:
        if REVIEWED_EXPANSIONS.get(name) != (new if name in added else old)[name]:
            raise ValueError("Migration change lacks an explicit reviewed N-1 compatibility edge")
    return bool(added)


def receipt_key(manifest: dict) -> str:
    sha = hashlib.sha256(json.dumps(contract(manifest), sort_keys=True).encode()).hexdigest()
    return f"compatibility/{sha}/migration.json"


def verify_receipt(value: dict, parameters: dict, manifest: dict) -> None:
    expected = {
        "contract": contract(manifest),
        "database_secret": parameters["DatabaseMigratorSecretArn"],
        "status": "complete",
        "account": "669409472143",
        "region": "ap-south-2",
        "cluster": "sanchezcloud-personal",
    }
    if any(value.get(key) != item for key, item in expected.items()):
        raise ValueError("No completed compatibility migration for this database and contract")
