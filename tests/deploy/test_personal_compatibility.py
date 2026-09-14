"""A reviewed expansion never weakens immutable checksums or Identity ownership."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module():
    spec = importlib.util.spec_from_file_location(
        "compatibility", ROOT / "scripts/personal_compatibility.py"
    )
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def contracts(m):
    old = {"identity_revision": "a" * 40, "migrations": {"migrations/001.sql": "b" * 64}}
    new = old | {"migrations": old["migrations"] | m.REVIEWED_EXPANSIONS}
    return old, new


def test_only_reviewed_append_requires_an_execution_receipt_and_allows_n_minus_one():
    m = module()
    old, new = contracts(m)
    assert m.expansion_required(old, new)
    assert not m.expansion_required(new, old)
    assert not m.expansion_required(new, new)


def test_changed_prior_checksum_identity_or_unreviewed_addition_is_rejected():
    m = module()
    old, new = contracts(m)
    for bad in [
        new | {"identity_revision": "d" * 40},
        new | {"migrations": new["migrations"] | {"migrations/001.sql": "c" * 64}},
        new | {"migrations": new["migrations"] | {"migrations/017_unreviewed.sql": "d" * 64}},
        new | {"migrations": old["migrations"] | {next(iter(m.REVIEWED_EXPANSIONS)): "e" * 64}},
    ]:
        with pytest.raises(ValueError):
            m.expansion_required(old, bad)


def test_receipt_must_prove_this_database_and_complete_contract():
    m = module()
    _, new = contracts(m)
    parameters = {"DatabaseMigratorSecretArn": "database-secret"}
    good = {
        "contract": new,
        "database_secret": "database-secret",
        "status": "complete",
        "account": "669409472143",
        "region": "ap-south-2",
        "cluster": "sanchezcloud-personal",
    }
    m.verify_receipt(good, parameters, new)
    for bad in [
        good | {"database_secret": "other"},
        good | {"status": "running"},
        good | {"contract": {}},
        good | {"account": "919651863140"},
    ]:
        with pytest.raises(ValueError):
            m.verify_receipt(bad, parameters, new)
