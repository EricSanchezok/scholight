"""Deployment and static safety contracts for native ingestion."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def test_production_runs_two_native_ingestion_services() -> None:
    compose = yaml.safe_load((ROOT / "deploy/production/compose.yaml").read_text())
    services = compose["services"]

    assert services["metadata-sync"]["command"][-1] == "serve-sync"
    assert services["paper-ingest"]["command"][-1] == "serve-ingest"
    assert "profiles" not in services["metadata-sync"]
    assert "profiles" not in services["paper-ingest"]
    assert "volumes" not in services["api"]
    assert "volumes" not in services["metadata-sync"]
    assert services["paper-ingest"]["volumes"] == ["scholight-data:/data"]
    assert services["metadata-sync"]["restart"] == "unless-stopped"
    assert services["paper-ingest"]["restart"] == "unless-stopped"


def test_ingestion_code_has_no_collection_lifecycle_or_filter_delete() -> None:
    paths = [
        ROOT / "scholight/scheduler/metadata_sync.py",
        ROOT / "scholight/scheduler/ingest_worker.py",
        ROOT / "scholight/store/ingestion.py",
    ]
    source = "\n".join(path.read_text() for path in paths)

    assert "drop_collection" not in source
    assert "TRUNCATE TABLE" not in source.upper()
    assert "delete_arxiv_chunks_by_paper" not in source
    assert ".delete(" in source
    assert "ids=id_batch" in source
    assert (
        'client.delete(\n                    "arxiv_chunks",\n                    filter='
        not in source
    )


def test_legacy_daemons_and_checkpoints_are_removed() -> None:
    scheduler = ROOT / "scholight/scheduler"

    assert not (scheduler / "base.py").exists()
    assert not (scheduler / "pdf_download.py").exists()
    assert not (scheduler / "md_parse.py").exists()
    assert not (scheduler / "chunk_ingest.py").exists()


def test_release_smoke_requires_both_ingestion_services_running() -> None:
    smoke = (ROOT / "deploy/production/smoke.sh").read_text()

    assert 'retry "metadata-sync running" service_running metadata-sync' in smoke
    assert 'retry "paper-ingest running" service_running paper-ingest' in smoke
