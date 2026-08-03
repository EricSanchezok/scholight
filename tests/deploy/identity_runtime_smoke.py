"""Exercise SDK login and refresh with the runtime database role."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import asyncpg
from sanchezcloud_identity import (
    AsyncpgUserDatabase,
    AuthConfig,
    UserManager,
    hash_password,
)


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


async def _run() -> None:
    email = os.environ.get("IDENTITY_SMOKE_EMAIL", f"identity-smoke-{uuid4()}@example.com")
    password = "identity-runtime-smoke-password"
    pool = await asyncpg.create_pool(
        host=_required_env("SCHOLIGHT_PG_HOST"),
        port=int(os.environ.get("SCHOLIGHT_PG_PORT", "5432")),
        database=_required_env("SCHOLIGHT_PG_DATABASE"),
        user=_required_env("SCHOLIGHT_PG_USER"),
        password=_required_env("SCHOLIGHT_PG_PASSWORD"),
        ssl=False,
        min_size=1,
        max_size=1,
    )
    try:
        database = AsyncpgUserDatabase(lambda: pool)
        user_id = await database.create_user(email, await hash_password(password), None)
        async with pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE auth.users
                SET status = 'active', email_verified_at = now()
                WHERE id = $1
                """,
                user_id,
            )

        manager = UserManager(
            database,
            None,
            AuthConfig(client_id="scholight", jwt_secret="ci-runtime-smoke-secret-32-bytes"),
        )
        access_token, refresh_token = await manager.login(email, password)
        refreshed_access_token, refreshed_refresh_token = await manager.refresh_token(refresh_token)

        if not access_token or not refreshed_access_token:
            raise RuntimeError("SDK did not issue access tokens")
        if not refreshed_refresh_token or refreshed_refresh_token == refresh_token:
            raise RuntimeError("SDK did not rotate the refresh token")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(_run())
