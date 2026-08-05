"""Survey concurrency configuration contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scholight.config import Settings


def test_survey_concurrency_defaults_are_split_by_scope() -> None:
    loaded = Settings(_env_file=None)  # type: ignore[call-arg]

    assert loaded.survey_draft_global_concurrency == 64
    assert loaded.survey_job_global_concurrency == 16
    assert loaded.survey_draft_per_user_concurrency == 8
    assert loaded.survey_job_per_user_concurrency == 4
    assert loaded.survey_draft_worker_concurrency == 8
    assert loaded.survey_job_worker_concurrency == 1


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
