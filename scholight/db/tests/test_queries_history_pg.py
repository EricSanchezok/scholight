"""Isolated PostgreSQL integration tests for search-history consistency."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import asyncpg
import pytest

from scholight.db.client import DBError
from scholight.db.queries_history import bulk_soft_delete_search_entries, get_search_history

pytestmark = pytest.mark.pg_integration
_BASELINE = Path(__file__).parents[3] / "migrations/001_scholight_baseline.sql"


def _database_url() -> str:
    try:
        return os.environ["SCHOLIGHT_TEST_DATABASE_URL"]
    except KeyError as exc:
        raise RuntimeError("SCHOLIGHT_TEST_DATABASE_URL must target an isolated database") from exc


async def _reset_database(pool: asyncpg.Pool) -> None:
    await pool.execute("DROP SCHEMA IF EXISTS scholight CASCADE")
    await pool.execute("DROP SCHEMA IF EXISTS auth CASCADE")
    await pool.execute("CREATE SCHEMA auth")
    await pool.execute("CREATE SCHEMA scholight")
    await pool.execute("CREATE TABLE auth.users (id BIGINT PRIMARY KEY)")
    await pool.execute("INSERT INTO auth.users (id) VALUES (1), (2)")
    await pool.execute(_BASELINE.read_text(encoding="utf-8"))


async def _insert_history(
    pool: asyncpg.Pool,
    *,
    user_id: int,
    query: str,
    strength: str = "standard",
) -> int:
    return cast(
        int,
        await pool.fetchval(
            "INSERT INTO scholight.search_history "
            "(user_id, query_text, strength, filters, result_count, response_time_ms) "
            "VALUES ($1, $2, $3, NULL, 1, 1) RETURNING id",
            user_id,
            query,
            strength,
        ),
    )


class _PausingConnection:
    def __init__(
        self,
        connection: asyncpg.Connection,
        count_complete: asyncio.Event,
        resume_page: asyncio.Event,
    ) -> None:
        self._connection = connection
        self._count_complete = count_complete
        self._resume_page = resume_page

    def transaction(self, **kwargs: Any) -> Any:
        return self._connection.transaction(**kwargs)

    async def fetchrow(self, query: str, *args: Any) -> asyncpg.Record | None:
        row = await self._connection.fetchrow(query, *args)
        self._count_complete.set()
        await self._resume_page.wait()
        return row

    async def fetch(self, query: str, *args: Any) -> Sequence[asyncpg.Record]:
        return cast(Sequence[asyncpg.Record], await self._connection.fetch(query, *args))


class _PausingAcquire:
    def __init__(
        self,
        pool: asyncpg.Pool,
        count_complete: asyncio.Event,
        resume_page: asyncio.Event,
    ) -> None:
        self._pool = pool
        self._count_complete = count_complete
        self._resume_page = resume_page
        self._connection: asyncpg.Connection | None = None

    async def __aenter__(self) -> _PausingConnection:
        self._connection = await self._pool.acquire()
        return _PausingConnection(
            self._connection,
            self._count_complete,
            self._resume_page,
        )

    async def __aexit__(self, *_args: object) -> None:
        assert self._connection is not None
        await self._pool.release(self._connection)


class _PausingPool:
    def __init__(
        self,
        pool: asyncpg.Pool,
        count_complete: asyncio.Event,
        resume_page: asyncio.Event,
    ) -> None:
        self._pool = pool
        self._count_complete = count_complete
        self._resume_page = resume_page

    def acquire(self) -> _PausingAcquire:
        return _PausingAcquire(self._pool, self._count_complete, self._resume_page)


@pytest.mark.asyncio
async def test_history_page_and_total_share_repeatable_read_snapshot() -> None:
    pool = await asyncpg.create_pool(_database_url(), min_size=1, max_size=4)
    try:
        await _reset_database(pool)
        first_id = await _insert_history(pool, user_id=1, query="first")
        second_id = await _insert_history(pool, user_id=1, query="second", strength="thorough")
        await _insert_history(pool, user_id=2, query="other owner")

        count_complete = asyncio.Event()
        resume_page = asyncio.Event()
        pausing_pool = _PausingPool(pool, count_complete, resume_page)
        with patch("scholight.db.queries_history.get_pool", return_value=pausing_pool):
            page_task = asyncio.create_task(get_search_history(1, limit=20, offset=0))
            await count_complete.wait()
            async with pool.acquire() as writer, writer.transaction():
                await writer.execute(
                    "UPDATE scholight.search_history SET deleted_at = statement_timestamp() "
                    "WHERE id = $1",
                    first_id,
                )
                new_id = await writer.fetchval(
                    "INSERT INTO scholight.search_history "
                    "(user_id, query_text, strength, result_count, response_time_ms) "
                    "VALUES (1, 'new', 'standard', 1, 1) RETURNING id"
                )
            resume_page.set()
            snapshot_page = await page_task

        assert snapshot_page.total == 2
        assert {item.id for item in snapshot_page.items} == {first_id, second_id}
        with patch("scholight.db.queries_history.get_pool", return_value=pool):
            current_page = await get_search_history(1, limit=20, offset=0)

        assert current_page.total == 2
        assert {item.id for item in current_page.items} == {second_id, new_id}
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_literal_q_bulk_owner_isolation_rollback_and_replay() -> None:
    pool = await asyncpg.create_pool(_database_url(), min_size=1, max_size=4)
    try:
        await _reset_database(pool)
        literal_id = await _insert_history(pool, user_id=1, query=r"literal 100%_match\done")
        wildcard_id = await _insert_history(pool, user_id=1, query="literal 100XXmatchdone")
        second_id = await _insert_history(pool, user_id=1, query="second")
        other_owner_id = await _insert_history(pool, user_id=2, query=r"literal 100%_match\done")

        with patch("scholight.db.queries_history.get_pool", return_value=pool):
            literal_page = await get_search_history(1, q="100%_match\\")

        assert literal_page.total == 1
        assert [item.id for item in literal_page.items] == [literal_id]

        await pool.execute(
            "CREATE FUNCTION scholight.reject_second_history_delete() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            f"IF OLD.id = $trigger_id${second_id}$trigger_id$ "
            "THEN RAISE EXCEPTION 'forced'; END IF; "
            "RETURN NEW; END $$"
        )
        await pool.execute(
            "CREATE TRIGGER reject_second_history_delete BEFORE UPDATE "
            "ON scholight.search_history "
            "FOR EACH ROW EXECUTE FUNCTION scholight.reject_second_history_delete()"
        )
        with (
            patch("scholight.db.queries_history.get_pool", return_value=pool),
            pytest.raises(DBError),
        ):
            await bulk_soft_delete_search_entries(1, [literal_id, second_id])
        assert (
            await pool.fetchval(
                "SELECT count(*) FROM scholight.search_history "
                "WHERE id = ANY($1::bigint[]) AND deleted_at IS NULL",
                [literal_id, second_id],
            )
            == 2
        )

        await pool.execute("DROP TRIGGER reject_second_history_delete ON scholight.search_history")
        await pool.execute("DROP FUNCTION scholight.reject_second_history_delete()")
        with patch("scholight.db.queries_history.get_pool", return_value=pool):
            deleted = await bulk_soft_delete_search_entries(
                1,
                [literal_id, second_id, wildcard_id, other_owner_id],
            )
            replayed = await bulk_soft_delete_search_entries(
                1,
                [literal_id, second_id, wildcard_id, other_owner_id],
            )

        assert (deleted, replayed) == (3, 0)
        assert (
            await pool.fetchval(
                "SELECT deleted_at IS NULL FROM scholight.search_history WHERE id = $1",
                other_owner_id,
            )
            is True
        )
    finally:
        await pool.close()
