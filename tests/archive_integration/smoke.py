"""Real Milvus/MinIO smoke. Endpoints are fixed to isolated loopback containers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import boto3
import click
from pymilvus import MilvusClient

from scholight.config import settings
from scholight.store.archive import (
    export_archive,
    initialize_archive_target,
    restore_archive,
    verify_archive,
    verify_restored,
)
from scholight.store.archive_io import ArchiveLocation
from scholight.store.schema import create_collections, create_indexes

SOURCE = "http://127.0.0.1:7253"
TARGET = "http://127.0.0.1:7254"
BUCKET = "scholight-archive-test"


def main() -> None:
    settings.runtime_profile = "full"
    source = MilvusClient(SOURCE)
    target = MilvusClient(TARGET)
    s3 = boto3.client(
        "s3",
        endpoint_url="http://127.0.0.1:7290",
        aws_access_key_id="archive-test",
        aws_secret_access_key="archive-test-only",
        region_name="us-east-1",
    )
    if BUCKET not in [bucket["Name"] for bucket in s3.list_buckets()["Buckets"]]:
        s3.create_bucket(Bucket=BUCKET)
    # Never clear an existing corpus, even in the test environment.
    if source.list_collections() or target.list_collections():
        raise RuntimeError(
            "Smoke requires fresh isolated containers; no collection will be deleted"
        )
    create_collections(source)
    create_indexes(source)
    papers = []
    chunks = []
    for index in range(259):
        arxiv_id = f"2401.{index:05d}"
        paper = {
            "arxiv_id": arxiv_id,
            "title": f"Paper {index}",
            "abstract": f"Retrieval and language models {index}",
            "authors": ["Example Author"],
            "categories": ["cs.IR"],
            "created": "2024-01-01",
            "updated": "2024-01-02",
            "version": 1,
            "updated_history": ["2024-01-01"],
            "license": "cc-by",
            "comments": "",
            "doi": "",
            "journal_ref": "",
            "acm_class": "",
            "has_latex": False,
            "has_pdf": False,
            "has_markdown": False,
            "has_chunks": True,
            "abstract_embedding": [0.25, -0.5] + [0.0] * (settings.embedding_dim - 2),
        }
        papers.append(paper)
        chunks.append(
            {
                "chunk_id": arxiv_id + "::chunk::0",
                "arxiv_id": arxiv_id,
                "chunk_idx": 0,
                "content_text": f"Full text {index}",
                "content_embedding": paper["abstract_embedding"],
            }
        )
    source.insert("arxiv_papers", papers)
    source.insert("arxiv_chunks", chunks)
    source.flush("arxiv_papers")
    source.flush("arxiv_chunks")
    for name in ("arxiv_papers", "arxiv_chunks"):
        source.load_collection(name)
    prefix = "s3://" + BUCKET + "/" + uuid4().hex
    results = {}
    upload = ArchiveLocation.upload
    for name in ("arxiv_papers", "arxiv_chunks"):
        location = prefix + "/" + name
        uploads = 0

        def interrupted_upload(storage: ArchiveLocation, path: Path, key: str) -> None:
            nonlocal uploads
            if key.endswith(".parquet"):
                uploads += 1
                if uploads == 2:
                    raise OSError("injected upload interruption")
            upload(storage, path, key)

        with patch.object(ArchiveLocation, "upload", interrupted_upload):
            try:
                export_archive(
                    source,
                    name,
                    location,
                    source_uri=SOURCE,
                    frozen=True,
                    s3_client=s3,
                    shard_bytes=8192,
                )
            except OSError:
                pass
            else:
                raise AssertionError("Expected interrupted export")
        try:
            verify_archive(location, s3_client=s3)
        except ValueError:
            pass
        else:
            raise AssertionError("Incomplete export accepted")
        exported = export_archive(
            source, name, location, source_uri=SOURCE, frozen=True, s3_client=s3, shard_bytes=8192
        )
        initialize_archive_target(target, name, location, target_uri=TARGET, s3_client=s3)
        restored = restore_archive(target, name, location, target_uri=TARGET, s3_client=s3)
        if name == "arxiv_papers" and target.has_collection("arxiv_chunks"):
            raise AssertionError("Papers-only restore created chunks")
        repeated = restore_archive(target, name, location, target_uri=TARGET, s3_client=s3)
        verified = verify_restored(target, name, location, target_uri=TARGET, s3_client=s3)
        results[name] = {
            "export": exported,
            "restore": restored,
            "repeat": repeated,
            "verify": verified,
        }
    output = Path(settings.data_root) / "archive-integration-result.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2))
    source.close()
    target.close()
    click.echo(json.dumps(results, sort_keys=True))


if __name__ == "__main__":
    main()
