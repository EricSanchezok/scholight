"""Drive the complete hermetic Survey flow and assert durable side effects."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any
from uuid import uuid4

import asyncpg
import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import ClientError

API = "http://api:8000"
MINIO = "http://minio:9000"
BUCKET = "scholight-survey-e2e"


async def _wait_http(client: httpx.AsyncClient, url: str, *, attempts: int = 120) -> None:
    for _ in range(attempts):
        try:
            response = await client.get(url)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(1)
    raise AssertionError(f"service did not become ready: {url}")


async def _poll_draft(
    client: httpx.AsyncClient,
    survey_id: str,
    *,
    expected_revision: int,
) -> dict[str, Any]:
    for _ in range(180):
        response = await client.get(f"{API}/surveys/{survey_id}/drafts")
        response.raise_for_status()
        drafts = response.json()
        ready = [
            item
            for item in drafts
            if item["status"] == "ready" and item["revision"] == expected_revision
        ]
        if ready:
            return ready[-1]
        if drafts and drafts[-1]["status"] in {"failed", "cancelled"}:
            raise AssertionError(f"Draft entered {drafts[-1]['status']}: {drafts[-1]}")
        await asyncio.sleep(1)
    raise AssertionError("Draft did not become ready")


async def _poll_survey(client: httpx.AsyncClient, survey_id: str) -> dict[str, Any]:
    observed: set[str] = set()
    for _ in range(240):
        progress = await client.get(f"{API}/surveys/{survey_id}/progress")
        progress.raise_for_status()
        observed.add(progress.json()["stage"])
        response = await client.get(f"{API}/surveys/{survey_id}")
        response.raise_for_status()
        survey = response.json()
        if survey["status"] == "succeeded":
            if not ({"waiting_for_execution", "finalizing", "completed"} & observed):
                raise AssertionError(f"no public progress stages were observed: {observed}")
            return survey
        if survey["status"] in {"failed", "cancelled"}:
            raise AssertionError(f"Survey entered {survey['status']}: {survey}")
        await asyncio.sleep(1)
    raise AssertionError("Survey did not finish")


def _ensure_bucket(s3: Any) -> None:
    try:
        s3.create_bucket(Bucket=BUCKET)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
            raise


def _read_object(s3: Any, key: str) -> bytes:
    response = s3.get_object(Bucket=BUCKET, Key=key)
    body = response["Body"]
    try:
        return body.read()
    finally:
        body.close()


async def _assert_database_and_archive(s3: Any, survey_id: str) -> None:
    connection = await asyncpg.connect(
        host="postgres",
        port=5432,
        database="survey_e2e",
        user="postgres",
        password="survey-local-only",
    )
    try:
        job = await connection.fetchrow(
            "SELECT id, manifest_key, terminal_outcome FROM scholight.survey_jobs "
            "WHERE survey_id = $1::uuid",
            survey_id,
        )
        assert job is not None
        assert job["terminal_outcome"] == "succeeded"
        manifest = json.loads(_read_object(s3, job["manifest_key"]))
        report = next(item for item in manifest["files"] if item["path"] == "run/08_survey.md")
        report_body = _read_object(s3, report["key"])
        assert hashlib.sha256(report_body).hexdigest() == report["sha256"]
        assert report_body.startswith(b"# Retrieval-Augmented Generation Survey")
        usage = await connection.fetchval(
            "SELECT used_count FROM scholight.user_daily_search_usage "
            "WHERE user_id = 42 AND strength = 'standard'"
        )
        histories = await connection.fetchval(
            "SELECT count(*) FROM scholight.search_history WHERE user_id = 42"
        )
        delegated = await connection.fetchval(
            "SELECT count(*) FROM scholight.usage_events "
            "WHERE user_id = 42 AND actor_type = 'delegated'"
        )
        quota = await connection.fetchrow(
            "SELECT reserved_count, succeeded_count FROM scholight.survey_daily_usage "
            "WHERE user_id = 42"
        )
        assert usage is not None and usage >= 2
        assert histories >= 2
        assert delegated >= 2
        assert quota is not None
        assert quota["reserved_count"] == 0
        assert quota["succeeded_count"] == 1
    finally:
        await connection.close()


async def main() -> None:
    timeout = httpx.Timeout(10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        await _wait_http(client, f"{API}/livez")
        await _wait_http(client, "http://model:8080/health")
        s3 = boto3.client(
            "s3",
            endpoint_url=MINIO,
            aws_access_key_id="minioadmin",
            aws_secret_access_key="minioadmin",
            region_name="ap-southeast-1",
            config=Config(s3={"addressing_style": "path"}),
        )
        await asyncio.to_thread(_ensure_bucket, s3)
        created = await client.post(
            f"{API}/surveys",
            json={
                "initial_request": "Survey retrieval-augmented generation evaluation",
                "client_request_id": str(uuid4()),
            },
        )
        created.raise_for_status()
        survey_id = created.json()["id"]
        first = await _poll_draft(client, survey_id, expected_revision=1)
        assert first["revision"] == 1
        revised = await client.post(
            f"{API}/surveys/{survey_id}/drafts",
            json={
                "message": "Emphasize benchmark quality and evidence.",
                "client_request_id": str(uuid4()),
            },
        )
        revised.raise_for_status()
        second = await _poll_draft(client, survey_id, expected_revision=2)
        assert second["revision"] == 2
        manual = await client.post(
            f"{API}/surveys/{survey_id}/drafts/manual",
            json={
                "markdown": second["markdown"] + "\n\nInclude reproducibility risks.",
                "message": "Manual final scope edit",
                "client_request_id": str(uuid4()),
            },
        )
        manual.raise_for_status()
        assert manual.json()["revision"] == 3
        started = await client.post(
            f"{API}/surveys/{survey_id}/start",
            json={"client_request_id": str(uuid4())},
        )
        started.raise_for_status()
        assert started.json()["status"] == "queued"
        await _poll_survey(client, survey_id)
        await _assert_database_and_archive(s3, survey_id)
        logging.getLogger(__name__).info("survey e2e passed")


if __name__ == "__main__":
    asyncio.run(main())
