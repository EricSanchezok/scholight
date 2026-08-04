"""Static safety contracts for bounded native ingestion on ECS."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]


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


def test_ecs_runs_bounded_ingestion_and_daily_metadata_tasks() -> None:
    template = (ROOT / "deploy/ecs/scholight-production.yml").read_text(encoding="utf-8")

    assert "Command: [scholight, scheduler, drain-ingest]" in template
    assert "ScheduleExpression: rate(2 hours)" in template
    assert "Command: [scholight, scheduler, sync]" in template
    assert "ScheduleExpression: cron(0 8 * * ? *)" in template
    assert template.count("State: !If [RunApplication, !Ref SchedulerState, DISABLED]") == 2


def test_ingest_image_is_separate_and_is_not_a_long_lived_service() -> None:
    dockerfile = (ROOT / "docker/scholight-api/Dockerfile").read_text(encoding="utf-8")
    template = (ROOT / "deploy/ecs/scholight-production.yml").read_text(encoding="utf-8")

    assert "FROM runtime-base AS ingest" in dockerfile
    assert 'CMD ["scholight", "scheduler", "drain-ingest"]' in dockerfile
    assert "IngestTaskDefinition:" in template
    assert "MetadataTaskDefinition:" in template
    assert "IngestService:" not in template
    assert "MetadataService:" not in template
