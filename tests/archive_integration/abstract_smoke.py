"""Real version-aware abstract copying between the isolated archive test collections."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import boto3
from pymilvus import MilvusClient

from scholight.config import settings
from scholight.store.fields import PAPER_ALL_FIELDS
from scholight.store.reconcile import AbstractReconciliation
from scholight.store.reconcile_inventory import build_delta, scan_inventory


def main() -> None:
    source = MilvusClient("http://127.0.0.1:7253")
    target = MilvusClient("http://127.0.0.1:7254")
    s3 = boto3.client(
        "s3",
        endpoint_url="http://127.0.0.1:7290",
        aws_access_key_id="archive-test",
        aws_secret_access_key="archive-test-only",
        region_name="us-east-1",
    )
    prefix = "s3://scholight-archive-test/abstract-" + uuid4().hex
    workspace = Path(settings.data_root) / "abstract-smoke"
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        rows = source.get(
            "arxiv_papers",
            ids=["2401.00000"],
            output_fields=[f for f in PAPER_ALL_FIELDS if f != "abstract_bm25"],
            consistency_level="Strong",
        )
        original = dict(rows[0])
        source.upsert(
            "arxiv_papers",
            data=[{"arxiv_id": "2401.00000", "version": 2, "has_chunks": False}],
            partial_update=True,
        )
        source.insert("arxiv_papers", [{**original, "arxiv_id": "2401.00999", "has_chunks": False}])
        with patch("boto3.client", return_value=s3):
            for label, client, uri in [
                ("source", source, "http://127.0.0.1:7253"),
                ("target", target, "http://127.0.0.1:7254"),
            ]:
                scan_inventory(
                    client,
                    prefix + "/" + label,
                    expected_uri=uri,
                    expected_id=str(client.describe_collection("arxiv_papers")["collection_id"]),
                    frozen=True,
                    workspace=workspace,
                )
            plan = build_delta(
                prefix + "/source", prefix + "/target", prefix + "/plan", workspace=workspace
            )
            assert plan["counts"]["insert"] == 1 and plan["counts"]["update"] == 1
            migration = AbstractReconciliation(
                source,
                target,
                prefix + "/plan",
                workspace=workspace,
                dimension=settings.embedding_dim,
                model=settings.embedding_model,
            )
            migration.apply()
            proof = migration.verify(frozen=True)
            revised = target.get(
                "arxiv_papers",
                ids=["2401.00000"],
                output_fields=["version", "has_chunks"],
                consistency_level="Strong",
            )[0]
            assert revised["version"] == 2 and revised["has_chunks"] is True
            assert proof["verified_candidates"] == 2 and proof["target_rows"] == 260
            (workspace / "result.json").write_text(json.dumps(proof, indent=2))
    finally:
        source.close()
        target.close()


if __name__ == "__main__":
    main()
