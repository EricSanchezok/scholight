"""Lean releases bind images and migration contracts to one merged source revision."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module():
    spec = importlib.util.spec_from_file_location("manifest", ROOT / "scripts/personal_manifest.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_manifest_rejects_foreign_images_wrong_architecture_and_changed_source(monkeypatch):
    m = module()
    contract = {"identity_revision": "b" * 40, "migrations": {"001.sql": "c" * 64}}
    monkeypatch.setattr(m, "source_contract", lambda sha: contract)
    monkeypatch.setattr(m, "require_merged", lambda sha: None)
    images = {
        name: f"669409472143.dkr.ecr.ap-south-2.amazonaws.com/scholight-personal-{name}@sha256:"
        + "d" * 64
        for name in m.COMPONENTS
    }
    good = m.create("a" * 40, "b" * 40, images)
    m.verify(good)
    for bad in [
        good | {"platform": "linux/amd64"},
        good | {"migrations": {}},
        good | {"images": images | {"api": images["api"].replace("669409472143", "919651863140")}},
    ]:
        with pytest.raises(ValueError):
            m.verify(bad)


def test_unmerged_revision_is_rejected_before_manifest_is_created(monkeypatch):
    m = module()

    def reject(sha):
        raise ValueError("not merged")

    monkeypatch.setattr(m, "require_merged", reject)
    with pytest.raises(ValueError, match="not merged"):
        m.create("a" * 40, "b" * 40, {})


def test_v2_requires_ingest_and_legacy_v1_remains_readable(monkeypatch):
    m = module()
    monkeypatch.setattr(m, "require_merged", lambda sha: None)
    monkeypatch.setattr(m, "source_contract", lambda sha: {"migrations": {}})
    images = {
        name: f"669409472143.dkr.ecr.ap-south-2.amazonaws.com/scholight-personal-{name}@sha256:"
        + "d" * 64
        for name in ("api", "web", "extract", "metadata", "ingest")
    }
    value = m.create("a" * 40, "b" * 40, images)
    assert value["version"] == 2
    old_images = {k: v for k, v in images.items() if k != "ingest"}
    m.verify(value | {"version": 1, "images": old_images})
    with pytest.raises(ValueError):
        m.verify(value | {"images": old_images})
