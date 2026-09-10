"""Database connection pinning contracts."""

from __future__ import annotations

import datetime as dt
import ssl
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import asyncpg
import pytest

from scholight.config import settings
from scholight.db import client as db_client
from scholight.db.queries_deferred_fulltext import record_deferred_fulltext


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["file", "pem", "disabled", "invalid"])
async def test_database_tls_preserves_file_support_and_verifies_inline_ca(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    certificate = ssl.DER_cert_to_PEM_cert(ssl.create_default_context().get_ca_certs(True)[0])
    ca = tmp_path / "ca.pem"
    ca.write_text(certificate)
    monkeypatch.setattr(db_client, "_pool", None)
    monkeypatch.setattr(settings, "pg_ssl_root_cert", str(ca) if mode == "file" else "disable")
    monkeypatch.setattr(
        settings,
        "pg_ssl_root_cert_pem",
        certificate if mode == "pem" else ("invalid certificate" if mode == "invalid" else ""),
    )
    create = AsyncMock(return_value=object())
    monkeypatch.setattr(asyncpg, "create_pool", create)
    if mode == "invalid":
        with pytest.raises(ssl.SSLError):
            await db_client.create_pool()
        create.assert_not_awaited()
        return
    await db_client.create_pool()
    context = create.call_args.kwargs["ssl"]
    if mode == "disabled":
        assert context is None
    else:
        assert isinstance(context, ssl.SSLContext)
        assert context.check_hostname
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.cert_store_stats()["x509_ca"] >= 1


class _Acquire:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    async def __aenter__(self) -> object:
        return self._connection

    async def __aexit__(self, *args: object) -> None:
        return None


class _Pool:
    def __init__(self, connection: object) -> None:
        self._connection = connection
        self.acquire_count = 0

    def acquire(self) -> _Acquire:
        self.acquire_count += 1
        return _Acquire(self._connection)


@pytest.mark.asyncio
async def test_bind_pool_connection_reuses_one_session_for_pool_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = AsyncMock()
    connection.execute.return_value = "SELECT 1"
    connection.fetchval.return_value = 7
    pool = _Pool(connection)
    monkeypatch.setattr(db_client, "_pool", None)

    async with db_client.bind_pool_connection(cast(Any, pool)) as acquired:
        assert acquired is connection
        assert await db_client.get_pool().execute("SELECT 1") == "SELECT 1"
        assert await db_client.get_pool().fetchval("SELECT 7") == 7
        async with db_client.get_pool().acquire() as reacquired:
            assert reacquired is connection

    assert pool.acquire_count == 1
    connection.execute.assert_awaited_once_with("SELECT 1", timeout=None)
    connection.fetchval.assert_awaited_once_with("SELECT 7", column=0, timeout=None)
    with pytest.raises(db_client.DBError, match="not initialised"):
        db_client.get_pool()


@pytest.mark.asyncio
async def test_deferred_ledger_batch_uses_the_locked_sync_session() -> None:
    connection = AsyncMock()
    pool = _Pool(connection)
    observed = dt.date(2026, 8, 4)
    async with db_client.bind_pool_connection(cast(Any, pool)):
        await record_deferred_fulltext([("2608.00001", 1), ("2608.00002", 3)], observed)
    assert pool.acquire_count == 1
    connection.executemany.assert_awaited_once()
    assert connection.executemany.call_args.args[1] == [
        ("2608.00001", 1, observed),
        ("2608.00002", 3, observed),
    ]


@pytest.mark.asyncio
async def test_failed_pinned_batch_propagates_and_releases_operation_lock() -> None:
    connection = AsyncMock()
    connection.executemany.side_effect = RuntimeError("batch rejected")
    pool = _Pool(connection)
    async with db_client.bind_pool_connection(cast(Any, pool)):
        with pytest.raises(RuntimeError, match="batch rejected"):
            await db_client.get_pool().executemany("INSERT batch", [(1,)], timeout=2)
        await db_client.get_pool().execute("SELECT 1")
    connection.executemany.assert_awaited_once_with("INSERT batch", [(1,)], timeout=2)
    connection.execute.assert_awaited_once_with("SELECT 1", timeout=None)
