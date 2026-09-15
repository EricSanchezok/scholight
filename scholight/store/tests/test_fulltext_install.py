"""Interrupted fulltext replacement must preserve the old revision and resume exactly."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from scholight.store.client import _WRITE_LOCK
from scholight.store.fulltext_install import FulltextInstall


class Iterator:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def next(self) -> list[dict[str, Any]]:
        batch, self.rows = self.rows[:2], self.rows[2:]
        return batch

    def close(self) -> None:
        pass


class Client:
    def __init__(self) -> None:
        self.paper: dict[str, Any] = {"arxiv_id": "2608.00001", "version": 2, "has_chunks": True}
        self.rows: dict[str, dict[str, Any]] = {
            "old": {
                "chunk_id": "old",
                "arxiv_id": "2608.00001",
                "chunk_idx": 0,
                "content_text": "old text",
                "content_embedding": [0.25, 0.5],
            }
        }
        self.writes = 0
        self.fail = False
        self.corrupt = False
        self.deleted: list[str] = []

    def query_iterator(self, _collection: str, **_kwargs: Any) -> Iterator:
        return Iterator(deepcopy(list(self.rows.values())))

    def get(self, name: str, ids: list[str], **_kwargs: Any) -> list[dict[str, Any]]:
        if name == "arxiv_papers":
            return [deepcopy(self.paper)]
        result = deepcopy([self.rows[i] for i in ids if i in self.rows])
        if self.corrupt and result:
            result[0]["content_text"] = "corrupted"
        return result

    def upsert(self, name: str, data: list[dict[str, Any]], **_kwargs: Any) -> None:
        if name == "arxiv_papers":
            self.paper.update(data[0])
            return
        self.writes += 1
        if self.fail and self.writes == 2:
            raise OSError("write failed")
        self.rows.update({r["chunk_id"]: deepcopy(r) for r in data})

    def delete(self, _name: str, ids: list[str], **_kwargs: Any) -> None:
        self.deleted.extend(ids)
        for i in ids:
            self.rows.pop(i, None)


async def guard() -> None:
    pass


async def record(_stage: str, _manifest: dict[str, Any]) -> None:
    pass


def install(client: Client, root: Path) -> FulltextInstall:
    return FulltextInstall(
        client,
        target_id="a" * 64,
        arxiv_id="2608.00001",
        version=2,
        profile="b" * 64,
        dimension=2,
        location=str(root / "archive"),
        workspace=root / "work",
        guard=guard,
        record=record,
    )


def chunks() -> list[dict[str, Any]]:
    return [
        {
            "chunk_id": f"new-{i}",
            "arxiv_id": "2608.00001",
            "chunk_idx": i,
            "content_text": f"new {i}",
            "content_embedding": [0.5, 0.75],
        }
        for i in range(65)
    ]


@pytest.mark.asyncio
async def test_shared_client_writes_hold_lock_during_parallel_install(tmp_path, monkeypatch):
    client = Client()
    original_upsert, original_delete = client.upsert, client.delete
    writes = []

    def upsert(*args, **kwargs):
        assert _WRITE_LOCK.locked()
        writes.append("upsert")
        return original_upsert(*args, **kwargs)

    def delete(*args, **kwargs):
        assert _WRITE_LOCK.locked()
        writes.append("delete")
        return original_delete(*args, **kwargs)

    monkeypatch.setattr(client, "upsert", upsert)
    monkeypatch.setattr(client, "delete", delete)
    task = install(client, tmp_path)
    await task.prepare(chunks(), {"has_markdown": True})
    await task.apply()
    assert writes == ["upsert", "upsert", "delete", "upsert"]


@pytest.mark.asyncio
async def test_partial_write_keeps_old_chunks_and_resumes_from_saved_manifest(
    tmp_path: Path,
) -> None:
    client = Client()
    task = install(client, tmp_path)
    await task.prepare(chunks(), {"has_markdown": True})
    client.fail = True
    with pytest.raises(OSError):
        await task.apply()
    assert "old" in client.rows and not client.deleted
    client.fail = False
    resumed = install(client, tmp_path)
    result = await resumed.apply()
    assert result["chunk_count"] == 65 and client.deleted == ["old"]
    assert len(client.rows) == 65
    await resumed.apply()
    assert len(client.rows) == 65


@pytest.mark.asyncio
async def test_corrupt_readback_never_deletes_old_chunks(tmp_path: Path) -> None:
    client = Client()
    task = install(client, tmp_path)
    await task.prepare(chunks(), {})
    client.corrupt = True
    with pytest.raises(ValueError, match="verification"):
        await task.apply()
    assert "old" in client.rows and not client.deleted


@pytest.mark.asyncio
async def test_corrupt_archive_or_changed_version_refuses_writes(tmp_path: Path) -> None:
    client = Client()
    task = install(client, tmp_path)
    await task.prepare(chunks(), {})
    next((tmp_path / "archive").glob("new-*.parquet")).write_bytes(b"broken")
    with pytest.raises(ValueError, match="checksum"):
        await task.apply()
    assert client.writes == 0


@pytest.mark.asyncio
async def test_lost_lease_refuses_cleanup(tmp_path: Path) -> None:
    client = Client()
    task = install(client, tmp_path)
    await task.prepare(chunks(), {})

    async def lost() -> None:
        raise RuntimeError("lost lease")

    task.guard = lost
    with pytest.raises(RuntimeError, match="lost lease"):
        await task.apply()
    assert not client.deleted and client.writes == 0


@pytest.mark.asyncio
async def test_revision_change_keeps_old_chunks(tmp_path: Path) -> None:
    client = Client()
    task = install(client, tmp_path)
    await task.prepare(chunks(), {})
    client.paper["version"] = 3
    with pytest.raises(ValueError, match="version changed"):
        await task.apply()
    assert client.writes == 0 and not client.deleted


@pytest.mark.asyncio
async def test_disk_shortage_cannot_commit_or_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    import scholight.store.fulltext_install as module

    client = Client()
    task = install(client, tmp_path)
    monkeypatch.setattr(module.shutil, "disk_usage", lambda _path: SimpleNamespace(free=1))
    with pytest.raises(OSError, match="disk space"):
        await task.prepare(chunks(), {})
    assert not await task.exists() and client.writes == 0


@pytest.mark.asyncio
async def test_cancellation_waits_for_the_inflight_write_to_settle() -> None:
    import asyncio
    import threading

    from scholight.store.fulltext_install import settled_io

    entered = threading.Event()
    finish = threading.Event()
    completed = threading.Event()

    def write() -> None:
        entered.set()
        assert finish.wait(timeout=2)
        completed.set()

    task = asyncio.create_task(settled_io(write))
    await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed.is_set()
