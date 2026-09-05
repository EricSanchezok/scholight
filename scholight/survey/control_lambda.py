"""AWS Lambda image entry point for the Survey control plane."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import boto3

from scholight.config import settings
from scholight.db.client import close_pool, create_pool
from scholight.logging import configure_logging
from scholight.survey.control import SurveyControl, SurveyControlConfig
from scholight.survey.email_notifications import AliyunSurveyEmailSender

_DATABASE_FIELDS = {
    "host": "pg_host",
    "port": "pg_port",
    "dbname": "pg_database",
    "username": "pg_user",
    "password": "pg_password",  # nosec B105 -- Secrets Manager JSON field name.
}
_MAIL_FIELDS = {
    "aliyun_access_key_id": "aliyun_dm_access_key_id",
    # This is a Secrets Manager JSON field name, not a credential value.
    "aliyun_access_key_secret": "aliyun_dm_access_key_secret",  # nosec B105
    "aliyun_account_name": "aliyun_dm_account_name",
}


def handler(event: dict[str, Any], context: object) -> dict[str, int]:
    """Process one event/tick; reserved concurrency serializes all cycles."""
    del context
    configure_logging(log_level=settings.log_level, use_json=True)
    return asyncio.run(_run(event))


async def _run(event: dict[str, Any]) -> dict[str, int]:
    secrets = boto3.client("secretsmanager")
    _apply_secret(
        _read_secret(secrets, _required_env("SURVEY_DATABASE_SECRET_ARN")),
        _DATABASE_FIELDS,
    )
    _apply_secret(
        _read_secret(secrets, _required_env("SURVEY_MAIL_SECRET_ARN")),
        _MAIL_FIELDS,
    )
    settings.pg_pool_min_size = 1
    settings.pg_pool_max_size = 1
    await create_pool()
    try:
        sender = AliyunSurveyEmailSender(
            access_key_id=settings.aliyun_dm_access_key_id,
            access_key_secret=settings.aliyun_dm_access_key_secret,
            account_name=settings.aliyun_dm_account_name,
            from_alias=settings.aliyun_dm_from_alias,
            reply_to_address=settings.aliyun_dm_reply_to_address,
        )
        control = SurveyControl(
            config=SurveyControlConfig(
                cluster_arn=_required_env("SURVEY_ECS_CLUSTER_ARN"),
                draft_task_definition_arn=_required_env("SURVEY_DRAFT_TASK_DEFINITION_ARN"),
                full_task_definition_arn=_required_env("SURVEY_FULL_TASK_DEFINITION_ARN"),
                full_high_memory_task_definition_arn=_required_env(
                    "SURVEY_FULL_HIGH_TASK_DEFINITION_ARN"
                ),
                subnet_ids=tuple(
                    value for value in _required_env("SURVEY_SUBNET_IDS").split(",") if value
                ),
                security_group_id=_required_env("SURVEY_SECURITY_GROUP_ID"),
                draft_global_concurrency=settings.survey_draft_global_concurrency,
                full_global_concurrency=settings.survey_job_global_concurrency,
                full_per_user_concurrency=settings.survey_job_per_user_concurrency,
            ),
            ecs_client=boto3.client("ecs"),
            email_sender=sender,
        )
        return await control.run_cycle(event)
    finally:
        await close_pool()


def _read_secret(client: Any, secret_arn: str) -> dict[str, object]:
    response = client.get_secret_value(SecretId=secret_arn)
    value = response.get("SecretString")
    if not isinstance(value, str):
        raise RuntimeError("Survey control secret must use SecretString")
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise RuntimeError("Survey control secret must contain a JSON object")
    return parsed


def _apply_secret(values: dict[str, object], mapping: dict[str, str]) -> None:
    for source, target in mapping.items():
        value = values.get(source)
        if value is None or value == "":
            raise RuntimeError(f"Survey control secret is missing {source}")
        setattr(settings, target, value)


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


__all__ = ["handler"]
