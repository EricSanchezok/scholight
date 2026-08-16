"""Minimal, secret-scoped environment for Survey RCM subprocesses."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt

from scholight.config import settings


def delegated_authorization(
    *,
    user_id: int,
    lifetime_seconds: int,
    survey_job_id: UUID | None = None,
) -> str:
    now = datetime.now(UTC)
    claims = {
        "iss": "scholight-survey",
        "aud": "scholight-mcp",
        "sub": str(user_id),
        "scope": "search",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=lifetime_seconds, minutes=15)).timestamp()),
        "jti": str(uuid4()),
    }
    if survey_job_id is not None:
        claims["survey_job_id"] = str(survey_job_id)
    claims["scope"] = "mcp"
    token = jwt.encode(claims, settings.survey_mcp_jwt_secret, algorithm="HS256")
    return f"Bearer {token}"


def survey_environment(
    *,
    user_id: int,
    lifetime_seconds: int,
    include_image: bool,
    survey_job_id: UUID | None = None,
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
            survey_job_id=survey_job_id,
        ),
    }
    if include_image and settings.image_gen_api_key:
        environment["IMAGE_GEN_API_KEY"] = settings.image_gen_api_key
        if settings.image_gen_api_url:
            environment["IMAGE_GEN_API_URL"] = settings.image_gen_api_url
        if settings.image_gen_trusted_hosts:
            environment["IMAGE_GEN_TRUSTED_HOSTS"] = settings.image_gen_trusted_hosts
    for name in ("SSL_CERT_DIR", "SSL_CERT_FILE", "TZ"):
        if value := os.environ.get(name):
            environment[name] = value
    return environment


def image_canary_environment() -> dict[str, str]:
    """Return only the process settings needed by the fixed image canary."""
    environment = {
        "HOME": os.environ.get("HOME", "/home/scholight"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    }
    for name, value in (
        ("IMAGE_GEN_API_KEY", settings.image_gen_api_key),
        ("IMAGE_GEN_API_URL", settings.image_gen_api_url),
        ("IMAGE_GEN_TRUSTED_HOSTS", settings.image_gen_trusted_hosts),
    ):
        if value:
            environment[name] = value
    for name in ("SSL_CERT_DIR", "SSL_CERT_FILE", "TZ"):
        if env_value := os.environ.get(name):
            environment[name] = env_value
    return environment


__all__ = ["delegated_authorization", "image_canary_environment", "survey_environment"]
