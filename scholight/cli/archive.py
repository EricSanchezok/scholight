"""Explicit collection archive operations; never deletes source or target data."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import click

from scholight.config import settings
from scholight.store.client import connect

COLLECTION = click.Choice(["arxiv_papers", "arxiv_chunks"])


def _checked_client(expected_uri: str) -> Any:
    if os.environ.get("SCHOLIGHT_DISABLE_DOTENV") != "1":
        raise click.ClickException(
            "Set SCHOLIGHT_DISABLE_DOTENV=1 and inject connection settings explicitly"
        )
    if not settings.zilliz_uri or settings.zilliz_uri.rstrip("/") != expected_uri.rstrip("/"):
        raise click.ClickException(
            "Expected URI does not match the explicitly configured Zilliz endpoint"
        )
    return connect()


@click.command("export")
@click.option("--collection", type=COLLECTION, multiple=True, required=True)
@click.option(
    "--destination", required=True, help="Dedicated local or s3://bucket/prefix archive root."
)
@click.option(
    "--expect-uri", required=True, help="Exact source endpoint; must match injected settings."
)
@click.option(
    "--source-frozen",
    is_flag=True,
    help="Assert all source writers are stopped for this export and resume.",
)
def export_cmd(
    collection: tuple[str, ...], destination: str, expect_uri: str, source_frozen: bool
) -> None:
    """Archive explicitly selected collections, each under its own subdirectory."""
    from scholight.store.archive import export_archive

    client = _checked_client(expect_uri)
    for name in dict.fromkeys(collection):
        result = export_archive(
            client,
            name,
            destination.rstrip("/") + "/" + name,
            source_uri=expect_uri,
            frozen=source_frozen,
        )
        click.echo(json.dumps({"collection": name, **result}, sort_keys=True))


@click.command("verify")
@click.argument("archive")
def verify_cmd(archive: str) -> None:
    """Verify a collection archive without connecting to Zilliz."""
    from scholight.store.archive import verify_archive

    click.echo(json.dumps(verify_archive(archive), sort_keys=True))


@click.command("init-archive")
@click.argument("archive")
@click.option("--collection", type=COLLECTION, required=True)
@click.option("--expect-uri", required=True)
def initialize_cmd(archive: str, collection: str, expect_uri: str) -> None:
    """Initialize one empty target from saved schema, functions and indexes."""
    from scholight.store.archive import initialize_archive_target

    initialize_archive_target(
        _checked_client(expect_uri), collection, archive, target_uri=expect_uri
    )
    click.echo("Selected empty collection initialized; no data imported.")


@click.command("restore")
@click.argument("archive")
@click.option("--collection", type=COLLECTION, required=True)
@click.option(
    "--expect-uri", required=True, help="Exact target endpoint; never the source endpoint."
)
@click.option(
    "--legacy-jsonl",
    is_flag=True,
    help="Read an unverified legacy local backup into an empty target.",
)
def restore_cmd(archive: str, collection: str, expect_uri: str, legacy_jsonl: bool) -> None:
    """Restore one selected collection; never imports the sibling collection."""
    client = _checked_client(expect_uri)
    if legacy_jsonl:
        from scholight.store.export import restore_collection_from_path

        count = client.query(
            collection, filter="", output_fields=["count(*)"], consistency_level="Strong"
        )
        if int(count[0]["count(*)"]) != 0:
            raise click.ClickException("Legacy restore requires an empty target")
        total = restore_collection_from_path(client, collection, Path(archive))
        result = {"rows": total, "final_archive": False, "restoration_verified": False}
    else:
        from scholight.store.archive import restore_archive

        result = restore_archive(client, collection, archive, target_uri=expect_uri)
    click.echo(json.dumps(result, sort_keys=True))


@click.command("verify-restored")
@click.argument("archive")
@click.option("--collection", type=COLLECTION, required=True)
@click.option("--expect-uri", required=True)
def verify_restored_cmd(archive: str, collection: str, expect_uri: str) -> None:
    """Compare every restored row and vector with a verified archive."""
    from scholight.store.archive import verify_restored

    result = verify_restored(
        _checked_client(expect_uri), collection, archive, target_uri=expect_uri
    )
    click.echo(json.dumps(result, sort_keys=True))
