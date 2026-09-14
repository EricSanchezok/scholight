"""Explicit, destination-locked abstract reconciliation; tokens never enter CLI arguments."""

from __future__ import annotations

import json
import os
from pathlib import Path

import click
from pymilvus import MilvusClient

from scholight.config import settings
from scholight.models.ingestion_target import digest_json


@click.command("reconcile")
@click.argument("operation", type=click.Choice(["plan", "apply", "verify"]))
@click.option("--destination", required=True, help="Dedicated encrypted S3 reconciliation prefix.")
@click.option("--source-uri", required=True)
@click.option("--source-collection-id", required=True)
@click.option("--target-uri", required=True)
@click.option("--target-collection-id", required=True)
@click.option("--source-frozen", is_flag=True)
@click.option("--target-frozen", is_flag=True)
@click.option(
    "--source-count-duplicate-id",
    multiple=True,
    help="Audited physical-count ID; never copied by the delta.",
)
@click.option(
    "--target-count-duplicate-id",
    multiple=True,
    help="Audited physical-count ID; never copied by the delta.",
)
@click.option("--model", required=True)
@click.option("--dimension", required=True, type=click.IntRange(1, 32768))
def reconcile_cmd(
    operation: str,
    destination: str,
    source_uri: str,
    source_collection_id: str,
    target_uri: str,
    target_collection_id: str,
    source_frozen: bool,
    target_frozen: bool,
    model: str,
    dimension: int,
    source_count_duplicate_id: tuple[str, ...],
    target_count_duplicate_id: tuple[str, ...],
) -> None:
    """Copy only papers after both writers stop; preserves destination fulltext state."""
    if os.environ.get("SCHOLIGHT_DISABLE_DOTENV") != "1":
        raise click.UsageError("Disable dotenv and explicitly inject reconciliation credentials")
    if operation != "plan" and (source_count_duplicate_id or target_count_duplicate_id):
        raise click.UsageError(
            "Duplicate count IDs belong to plan; apply and verify use its evidence"
        )
    if not destination.startswith("s3://") or not source_frozen or not target_frozen:
        raise click.UsageError(
            "A dedicated S3 prefix and both frozen-writer declarations are required"
        )
    if source_uri.rstrip("/") == target_uri.rstrip("/"):
        raise click.UsageError("Source and target endpoints must differ")
    tokens = [
        os.environ.get("SCHOLIGHT_RECONCILE_SOURCE_TOKEN"),
        os.environ.get("SCHOLIGHT_RECONCILE_TARGET_TOKEN"),
    ]
    if not all(tokens):
        raise click.UsageError(
            "Inject SCHOLIGHT_RECONCILE_SOURCE_TOKEN and SCHOLIGHT_RECONCILE_TARGET_TOKEN"
        )
    from scholight.store.reconcile import AbstractReconciliation
    from scholight.store.reconcile_inventory import build_delta, scan_inventory

    clients = []
    try:
        for uri, token, identity in [
            (source_uri, tokens[0], source_collection_id),
            (target_uri, tokens[1], target_collection_id),
        ]:
            client = MilvusClient(uri=uri, token=token, timeout=30)
            clients.append(client)
            description = client.describe_collection("arxiv_papers")
            if str(description["collection_id"]) != identity:
                raise ValueError("Actual collection identity differs from the reviewed command")
            vector = next(f for f in description["fields"] if f["name"] == "abstract_embedding")
            if int(vector["type"]) != 101 or int(vector["params"]["dim"]) != dimension:
                raise ValueError("Collection vector type or dimension mismatch")
        source, target = clients
        workspace = Path(settings.data_root) / "reconciliation"
        prefix = destination.rstrip("/")
        if operation == "plan":
            for label, client, uri, identity in [
                ("source", source, source_uri, source_collection_id),
                ("target", target, target_uri, target_collection_id),
            ]:
                scan_inventory(
                    client,
                    prefix + "/" + label,
                    expected_uri=uri,
                    expected_id=identity,
                    frozen=True,
                    workspace=workspace,
                    count_duplicate_ids=(
                        source_count_duplicate_id
                        if label == "source"
                        else target_count_duplicate_id
                    ),
                )
            result = build_delta(
                prefix + "/source", prefix + "/target", prefix + "/plan", workspace=workspace
            )
            click.echo(
                json.dumps(
                    {
                        "operation": "plan",
                        "counts": result["counts"],
                        "candidates": result["candidates"],
                    },
                    sort_keys=True,
                )
            )
        else:
            migration = AbstractReconciliation(
                source,
                target,
                prefix + "/plan",
                workspace=workspace,
                dimension=dimension,
                model=model,
            )
            if migration.plan["binding"]["source"] != {
                "uri": source_uri.rstrip("/"),
                "collection_id": source_collection_id,
            } or migration.plan["binding"]["target"] != {
                "uri": target_uri.rstrip("/"),
                "collection_id": target_collection_id,
            }:
                raise ValueError("Plan differs from explicit source and target bindings")
            result = migration.apply() if operation == "apply" else migration.verify(frozen=True)
            click.echo(
                json.dumps(
                    {
                        "operation": operation,
                        "complete": result["complete"],
                        "verified_candidates": result["verified_candidates"],
                        **(
                            {"verification_sha256": digest_json(result)}
                            if operation == "verify"
                            else {}
                        ),
                    },
                    sort_keys=True,
                )
            )
    except Exception as exc:
        # SDK diagnostics may contain credential-bearing connection details.
        raise click.ClickException(
            "Reconciliation failed ("
            + type(exc).__name__
            + "); committed recovery state is retained"
        ) from None
    finally:
        for client in clients:
            client.close()
