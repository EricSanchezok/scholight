"""Isolated API wiring for the real-runtime Survey E2E."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import asyncpg
from fastapi import Header
from sanchezcloud_identity.models.user import UserRecord

from scholight.api.deps import get_current_user
from scholight.config import settings
from scholight.db.migrate import apply_migrations
from scholight.models.search import SearchHit, SearchResult
from scholight.search.engine import SearchEngine


async def _bootstrap_database() -> None:
    connection = await asyncpg.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        database=settings.pg_database,
        user=settings.pg_user,
        password=settings.pg_password,
        ssl=False,
    )
    try:
        await connection.execute("CREATE SCHEMA IF NOT EXISTS auth")
        await connection.execute("CREATE SCHEMA IF NOT EXISTS scholight")
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS auth.users (
                id BIGINT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                display_name TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                email_verified_at TIMESTAMPTZ,
                email_verify_token TEXT,
                email_verify_expires_at TIMESTAMPTZ,
                password_reset_token TEXT,
                password_reset_expires_at TIMESTAMPTZ,
                failed_login_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until TIMESTAMPTZ,
                last_login_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await apply_migrations(connection)
        await connection.executemany(
            """
            INSERT INTO auth.users (
                id, email, password_hash, display_name, status, email_verified_at
            ) VALUES ($1, $2, 'not-a-real-hash', $3, 'active', now())
            ON CONFLICT (id) DO NOTHING
            """,
            [
                (user_id, f"survey-e2e-{user_id}@example.com", f"Survey E2E {user_id}")
                for user_id in [42, *range(100, 105)]
            ],
        )
        await connection.executemany(
            """
            INSERT INTO scholight.user_profiles (user_id, status)
            VALUES ($1, 'active')
            ON CONFLICT (user_id) DO NOTHING
            """,
            [(user_id,) for user_id in [42, *range(100, 105)]],
        )
    finally:
        await connection.close()


asyncio.run(_bootstrap_database())


async def _fake_search(self: SearchEngine, request: object) -> SearchResult:
    del self
    query = str(getattr(request, "query", "survey research"))
    return SearchResult(
        query=query,
        level=int(getattr(request, "level", 1)),
        total_ms=2.0,
        hits=[
            SearchHit(
                rank=1,
                score=0.98,
                arxiv_id="2401.00001",
                title="Deterministic Retrieval-Augmented Generation Evaluation",
                authors=["Scholight E2E"],
                abstract="A deterministic paper record used by the isolated Survey test.",
                categories=["cs.AI"],
                created="2024-01-01",
                updated="2024-01-01",
                version=1,
                updated_history=[],
                license="",
                comments="",
                doi="",
                journal_ref="",
                acm_class="",
            )
        ],
    )


def _fake_papers(
    arxiv_ids: list[str],
    *,
    categories: list[str] | None = None,
    authors: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    output_fields: list[str] | None = None,
    timeout: float | None = None,
) -> dict[str, dict[str, object]]:
    del categories, authors, date_from, date_to, output_fields, timeout
    return {
        arxiv_id: {
            "arxiv_id": arxiv_id,
            "abstract": "A deterministic paper record used by the isolated Survey test.",
        }
        for arxiv_id in arxiv_ids
    }


SearchEngine.search = _fake_search  # type: ignore[method-assign]
import scholight.api.search_execution as search_execution  # noqa: E402
import scholight.store.client as store_client  # noqa: E402

search_execution.batch_get_arxiv_papers = _fake_papers
store_client.get_client = lambda: object()

from scholight.api.app import create_app  # noqa: E402

app = create_app()


def _e2e_current_user(x_e2e_user_id: int = Header(default=42)) -> UserRecord:
    """Select one of the local fixture users without implementing test auth."""
    if x_e2e_user_id not in {42, *range(100, 105)}:
        x_e2e_user_id = 42
    return UserRecord(
        id=x_e2e_user_id,
        email=f"survey-e2e-{x_e2e_user_id}@example.com",
        password_hash="not-a-real-hash",
        display_name=f"Survey E2E {x_e2e_user_id}",
        status="active",
        email_verified=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


app.dependency_overrides[get_current_user] = _e2e_current_user
