"""A release must observe the actual controller pause and actual task termination."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "release_admission",
    Path(__file__).resolve().parents[2] / "scripts/release_admission.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_pause_acknowledgement_requires_fresh_loaded_parameter_versions():
    expected = {
        "scholight-metadata": {
            "parameter_version": 2,
            "enabled": False,
            "task_definition": "task",
        }
    }
    status = {
        "account": module.ACCOUNT,
        "region": module.REGION,
        "cluster": module.CLUSTER,
        "observed_at": 100,
        "registration_refreshed_at": 90,
        "registrations": expected,
    }
    assert module.acknowledged(status, expected, 110)
    for bad in [
        status | {"observed_at": 1},
        status | {"registration_refreshed_at": 0},
        status | {"registrations": {}},
        status | {"account": "919651863140"},
    ]:
        assert not module.acknowledged(bad, expected, 110)


def test_desired_stop_does_not_prove_a_worker_has_exited():
    task = {
        "group": "background:scholight-metadata",
        "desiredStatus": "STOPPED",
        "lastStatus": "STOPPING",
    }
    assert module.unsettled([task], {"scholight-metadata"})
    assert not module.unsettled([task | {"lastStatus": "STOPPED"}], {"scholight-metadata"})
    assert not module.unsettled([task], {"scholens-document"})


def test_registration_change_cannot_replace_or_delete_unrelated_resources():
    good = {
        "Changes": [
            {
                "ResourceChange": {
                    "Action": "Modify",
                    "LogicalResourceId": "AdmissionRegistration",
                    "Replacement": "False",
                }
            }
        ]
    }
    module.guard_registration_change(good)
    for update in [
        {"Action": "Remove"},
        {"LogicalResourceId": "Database"},
        {"Replacement": "True"},
    ]:
        with pytest.raises(ValueError):
            module.guard_registration_change(
                {"Changes": [{"ResourceChange": good["Changes"][0]["ResourceChange"] | update}]}
            )


def test_resume_after_ack_failure_does_not_repeat_runtime_or_pause_again():
    release = object.__new__(module.AdmissionRelease)
    release.arn = "change"
    release.operation = "operation"
    records = {
        "start": {"change_set": "change", "restore_enabled": "true"},
        "runtime": {"change_set": "change"},
        "revisions": {"DocumentTaskArn": "new"},
    }
    release.read = records.get
    release.record = lambda name, value: records.update({name: value})
    changes = []
    release.change_background = lambda stage, values: changes.append((stage, values))
    release.wait_ack = lambda: None
    release.run()
    release.run()
    assert changes == [("resume", {"MetadataEnabled": "true"})]
    assert "complete" in records


def test_absent_checkpoint_uses_explicit_prefix_listing_instead_of_masking_denial():
    release = object.__new__(module.AdmissionRelease)
    release.prefix = "cloudformation/personal/releases/example/"

    class Store:
        def list_objects_v2(self, **kwargs):
            assert kwargs["Prefix"] == release.prefix + "plan.json"
            assert kwargs["MaxKeys"] == 1
            return {"Contents": []}

        def get_object(self, **kwargs):
            raise AssertionError("An absent checkpoint must not need a forbidden GetObject")

    release.s3 = Store()
    assert release.read("plan") is None
