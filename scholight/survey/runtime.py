"""Minimal, secret-scoped environment for Survey RCM subprocesses."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt

from scholight.config import settings


def delegated_authorization(*, user_id: int, lifetime_seconds: int) -> str:
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "iss": "scholight-survey",
            "aud": "scholight-mcp",
            "sub": str(user_id),
            "scope": "mcp",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=lifetime_seconds, minutes=15)).timestamp()),
            "jti": str(uuid4()),
        },
        settings.survey_mcp_jwt_secret,
        algorithm="HS256",
    )
    return f"Bearer {token}"


def survey_environment(
    *, user_id: int, lifetime_seconds: int, include_image: bool
) -> dict[str, str]:
    environment = {
        "HOME": os.environ.get("HOME", "/home/scholight"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "NO_PROXY": "api,localhost,127.0.0.1",
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "DEEPSEEK_API_KEY": settings.deepseek_api_key,
        "SCHOLIGHT_SURVEY_MCP_AUTHORIZATION": delegated_authorization(
            user_id=user_id,
            lifetime_seconds=lifetime_seconds,
        ),
    }
    if include_image and settings.image_gen_api_key:
        environment["IMAGE_GEN_API_KEY"] = settings.image_gen_api_key
    for name in ("SSL_CERT_DIR", "SSL_CERT_FILE", "TZ"):
        if value := os.environ.get(name):
            environment[name] = value
    return environment


__all__ = ["delegated_authorization", "survey_environment"]
