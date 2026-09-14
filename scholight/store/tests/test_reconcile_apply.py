"""Abstract copying preserves destination fulltext and commits only verified batches."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from scholight.store.reconcile import AbstractReconciliation, merge_paper
from scholight.store.reconcile_inventory import build_delta
from scholight.store.tests.test_reconcile_inventory import InventoryClient, inventory, row


def paper(pk: str, version: int, *, chunks: bool = False) -> dict[str, Any]:
    return {
        **row(pk, version),
        "title": pk,
        "abstract": "text",
        "abstract_embedding": [0.25, 0.75],
        "has_chunks": chunks,
        "has_pdf": chunks,
        "has_latex": False,
        "has_markdown": chunks,
    }


class Client(InventoryClient):
    def __init__(self, rows: list[dict[str, Any]], identity: int) -> None:
        super().__init__(rows, identity)
        self.fail = False
        self.corrupt = False
        self.writes = 0

    def get(self, _name: str, ids: list[str], **_kwargs: Any) -> list[dict[str, Any]]:
        result = deepcopy([r for r in self.rows if r["arxiv_id"] in ids])
        if self.corrupt and result:
            result[0]["abstract_embedding"] = [1.0, 2.0]
        return result

    def upsert(self, name: str, data: list[dict[str, Any]], **kwargs: Any) -> None:
        assert name == "arxiv_papers"
        self.writes += 1
        if self.fail:
            raise OSError("injected write failure")
        values = {r["arxiv_id"]: r for r in self.rows}
        for r in data:
            if kwargs.get("partial_update"):
                values[r["arxiv_id"]].update(deepcopy(r))
            else:
                values[r["arxiv_id"]] = deepcopy(r)
        self.rows = list(values.values())


def setup(tmp_path: Path) -> tuple[AbstractReconciliation, Client, Client]:
    source = Client([paper("a", 2), paper("c", 1)], 1)
    target = Client([paper("a", 1, chunks=True), paper("b", 3, chunks=True)], 2)
    inventory(source, tmp_path / "source")
    inventory(target, tmp_path / "target")
    build_delta(
        str(tmp_path / "source"),
        str(tmp_path / "target"),
        str(tmp_path / "plan"),
        workspace=tmp_path / "scratch",
    )
    migration = AbstractReconciliation(
        source,
        target,
        str(tmp_path / "plan"),
        workspace=tmp_path / "work",
        dimension=2,
        model="Qwen/test",
    )
    return migration, source, target


def test_merge_never_downgrades_or_copies_lean_resource_flags() -> None:
    assert merge_paper(paper("a", 1), paper("a", 2, chunks=True)) is None
    merged = merge_paper(paper("a", 2), paper("a", 1, chunks=True))
    assert merged and merged["has_chunks"] and merged["has_pdf"]
    assert merged["abstract_embedding"] == [0.25, 0.75]


def test_apply_retries_prepared_batch_and_preserves_real_fulltext(tmp_path: Path) -> None:
    migration, source, target = setup(tmp_path)
    target.fail = True
    with pytest.raises(OSError):
        migration.apply()
    assert target.rows[0]["version"] == 1
    target.fail = False
    result = migration.apply()
    assert result["verified_candidates"] == 2
    by_id = {r["arxiv_id"]: r for r in target.rows}
    assert by_id["a"]["version"] == 2 and by_id["a"]["has_chunks"]
    assert by_id["b"]["version"] == 3 and by_id["b"]["has_chunks"]
    assert by_id["c"]["abstract_embedding"] == source.rows[1]["abstract_embedding"]
    assert migration.apply() == result
    proof = migration.verify(frozen=True)
    assert proof["target_rows"] == 3 and proof["complete"]
    assert proof["verified_candidates"] == 2


def test_corrupt_target_readback_cannot_commit_batch(tmp_path: Path) -> None:
    migration, _, target = setup(tmp_path)
    original = target.upsert

    def corrupt(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        target.corrupt = True

    target.upsert = corrupt  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="verification"):
        migration.apply()


def test_unexpected_target_edit_is_not_overwritten(tmp_path: Path) -> None:
    migration, _, target = setup(tmp_path)
    target.rows[0]["version"] = 4
    with pytest.raises(ValueError, match="changed"):
        migration.apply()
    assert target.writes == 0 and target.rows[0]["version"] == 4


def test_final_inventory_detects_deletion_outside_candidate_batch(tmp_path: Path) -> None:
    migration, _, target = setup(tmp_path)
    migration.apply()
    target.rows = [r for r in target.rows if r["arxiv_id"] != "b"]
    with pytest.raises(ValueError, match="missing or downgraded"):
        migration.verify(frozen=True)
