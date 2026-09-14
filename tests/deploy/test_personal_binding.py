"""A reviewed target binds credentials by version without reading their values."""

import importlib.util
import io
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module():
    spec = importlib.util.spec_from_file_location("binding", ROOT / "scripts/personal_binding.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def binding():
    return {
        "version": 1,
        "endpoint": "https://example.serverless.ali-cn-hangzhou.cloud.zilliz.com.cn",
        "papers_id": "123",
        "chunks_id": "456",
        "embedding_model": "Qwen/Qwen3-Embedding-0.6B",
        "embedding_dim": 1024,
        "secrets": {
            component: {
                "arn": "arn:aws:secretsmanager:ap-south-2:669409472143:secret:"
                + "/sanchezcloud/scholight/personal/des-"
                + component
                + "-Abc123",
                "version_id": str(index) * 32,
            }
            for index, component in enumerate(("api", "metadata", "ingest"), 1)
        },
        "recovery_uri": "s3://scholight-personal-releases-669409472143-ap-south-2/recovery/des/run1",
    }


def test_binding_requires_independent_pinned_credentials_and_expected_account():
    m = module()
    good = binding()
    result = m.parameters(good)
    assert len(result["IngestionTargetId"]) == 64
    assert result["SearchApiSecretVersion"] == "1" * 32
    for bad in [
        good | {"endpoint": good["endpoint"] + "?token=secret"},
        good | {"embedding_dim": 768},
        good | {"secrets": good["secrets"] | {"ingest": good["secrets"]["api"]}},
        good | {"recovery_uri": "s3://unrelated/recovery/des/run1"},
    ]:
        with pytest.raises(ValueError):
            m.parameters(bad)


def test_legacy_rollback_preserves_target_but_disables_both_consumers():
    m = module()
    current = m.parameters(binding()) | {"MetadataEnabled": "true", "IngestEnabled": "true"}
    result = m.release_parameters(current, {"version": 1, "images": {}})
    assert result["TargetEndpoint"] == current["TargetEndpoint"]
    assert result["RuntimeProfile"] == "lean"
    assert result["MetadataEnabled"] == result["IngestEnabled"] == "false"


def test_full_release_cannot_change_an_adopted_target_without_separate_reconciliation():
    m = module()
    current = m.parameters(binding())
    next_binding = binding() | {"papers_id": "789"}
    with pytest.raises(ValueError, match="target"):
        m.release_parameters(current, {"version": 2, "images": {}}, next_binding)


def test_first_target_binding_never_inherits_source_write_admission():
    m = module()
    values = m.release_parameters(
        {"MetadataEnabled": "true"}, {"version": 2, "images": {}}, binding()
    )
    assert values["MetadataEnabled"] == values["IngestEnabled"] == "false"


def test_ingestion_enable_requires_the_matching_adopted_target():
    m = module()

    class Store:
        def get_object(self, **kwargs):
            return {
                "Body": io.BytesIO(
                    json.dumps(
                        {
                            "format": "scholight.destination-adoption.v1",
                            "complete": True,
                            "target_id": "target",
                        }
                    ).encode()
                )
            }

    key = "recovery/des/run1/plan/adoption.json"
    assert len(m.read_adoption(Store(), key, "target")) == 64
    with pytest.raises(ValueError):
        m.read_adoption(Store(), key, "other-target")
