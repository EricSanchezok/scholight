"""Survey concurrency configuration contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from scholight.config import Settings

ROOT = Path(__file__).parents[3]


def test_survey_concurrency_defaults_are_split_by_scope() -> None:
    loaded = Settings(_env_file=None)  # type: ignore[call-arg]

    assert loaded.survey_draft_global_concurrency == 64
    assert loaded.survey_job_global_concurrency == 16
    assert loaded.survey_draft_per_user_concurrency == 8
    assert loaded.survey_job_per_user_concurrency == 4
    assert loaded.survey_draft_worker_concurrency == 8
    assert loaded.survey_job_worker_concurrency == 1
    assert loaded.survey_provider_max_attempts == 3
    assert loaded.survey_provider_retry_base_seconds == 2
    assert loaded.survey_provider_retry_max_seconds == 30
    assert loaded.survey_mcp_url == "http://api:8000/mcp"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://api.example/mcp",
        "https://user:secret@api.example/mcp",
        "https://api.example/mcp?token=secret",
        "https://api.example/mcp#fragment",
    ],
)
def test_survey_mcp_url_rejects_unsafe_endpoints(url: str) -> None:
    with pytest.raises(ValidationError, match="SCHOLIGHT_SURVEY_MCP_URL"):
        Settings(_env_file=None, survey_mcp_url=url)  # type: ignore[call-arg]


def test_environment_template_uses_only_explicit_concurrency_scopes() -> None:
    template = (ROOT / ".env.example").read_text(encoding="utf-8")

    for name, value in (
        ("SCHOLIGHT_SURVEY_DRAFT_GLOBAL_CONCURRENCY", 64),
        ("SCHOLIGHT_SURVEY_JOB_GLOBAL_CONCURRENCY", 16),
        ("SCHOLIGHT_SURVEY_DRAFT_PER_USER_CONCURRENCY", 8),
        ("SCHOLIGHT_SURVEY_JOB_PER_USER_CONCURRENCY", 4),
        ("SCHOLIGHT_SURVEY_DRAFT_WORKER_CONCURRENCY", 8),
        ("SCHOLIGHT_SURVEY_JOB_WORKER_CONCURRENCY", 1),
    ):
        assert f"{name}={value}" in template
    assert "SCHOLIGHT_SURVEY_DRAFT_CONCURRENCY=" not in template
    assert "SCHOLIGHT_SURVEY_JOB_CONCURRENCY=" not in template


@pytest.mark.parametrize(
    ("overrides", "setting_name"),
    [
        (
            {
                "survey_draft_global_concurrency": 4,
                "survey_draft_per_user_concurrency": 5,
            },
            "SCHOLIGHT_SURVEY_DRAFT_PER_USER_CONCURRENCY",
        ),
        (
            {
                "survey_job_global_concurrency": 2,
                "survey_job_per_user_concurrency": 2,
                "survey_job_worker_concurrency": 3,
            },
            "SCHOLIGHT_SURVEY_JOB_WORKER_CONCURRENCY",
        ),
    ],
)
def test_survey_concurrency_rejects_local_limits_above_global(
    overrides: dict[str, int],
    setting_name: str,
) -> None:
    with pytest.raises(ValidationError, match=setting_name):
        Settings(_env_file=None, **overrides)  # type: ignore[arg-type,call-arg]


def test_survey_provider_retry_rejects_inverted_backoff_bounds() -> None:
    with pytest.raises(
        ValidationError,
        match="SCHOLIGHT_SURVEY_PROVIDER_RETRY_BASE_SECONDS",
    ):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            survey_provider_retry_base_seconds=20,
            survey_provider_retry_max_seconds=10,
        )
