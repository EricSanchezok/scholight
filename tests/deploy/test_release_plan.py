"""Runtime apply binds a current controller, immutable manifest and unchanged stack."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("personal_apply", ROOT / "scripts/personal_apply.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def test_apply_rejects_expired_tampered_and_superseded_plans():
    plan = {
        "control": "a" * 40,
        "manifest_sha256": "b" * 64,
        "created_at": 100,
        "stack_updated_at": 90,
    }
    m.guard_plan(plan, "a" * 40, "b" * 64, 90, 110)
    for control, digest, updated, now in [
        ("c" * 40, "b" * 64, 90, 110),
        ("a" * 40, "c" * 64, 90, 110),
        ("a" * 40, "b" * 64, 101, 110),
        ("a" * 40, "b" * 64, 90, 90000),
    ]:
        with pytest.raises(ValueError):
            m.guard_plan(plan, control, digest, updated, now)


def test_candidate_rejects_unrelated_changes_after_pause():
    import release_admission as admission

    original = {"ApiImage": "old", "MetadataEnabled": "true", "DatabaseHost": "db"}
    desired = original | {"ApiImage": "new", "MetadataEnabled": "false"}
    admission.guard_candidate_state(original | {"MetadataEnabled": "false"}, original, desired)
    admission.guard_candidate_state(desired, original, desired)
    with pytest.raises(ValueError):
        admission.guard_candidate_state(desired | {"DatabaseHost": "other"}, original, desired)


def test_plan_rechecks_every_pinned_credential_without_reading_values():
    from personal_binding import verify_versions

    class Secrets:
        def describe_secret(self, **kwargs):
            assert kwargs["SecretId"].startswith("arn:expected:")
            return {"VersionIdsToStages": {"v1": ["AWSCURRENT"]}}

        def get_secret_value(self, **kwargs):
            raise AssertionError("Deployment controller must never read secret values")

    values = {
        prefix + "SecretArn": "arn:expected:" + prefix
        for prefix in ("SearchApi", "SearchSync", "SearchIngest")
    }
    values.update(
        {prefix + "SecretVersion": "v1" for prefix in ("SearchApi", "SearchSync", "SearchIngest")}
    )
    verify_versions(Secrets(), values)
    with pytest.raises(ValueError):
        verify_versions(Secrets(), values | {"SearchIngestSecretVersion": "missing"})
