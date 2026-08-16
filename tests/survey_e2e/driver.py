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
    user_id: int = 42,
) -> dict[str, Any]:
    for _ in range(180):
        response = await client.get(
            f"{API}/surveys/{survey_id}/drafts",
            headers={"X-E2E-User-Id": str(user_id)},
        )
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


async def _poll_survey(
    client: httpx.AsyncClient,
    survey_id: str,
    *,
    expected_status: str = "succeeded",
) -> dict[str, Any]:
    observed: set[str] = set()
    for _ in range(240):
        progress = await client.get(f"{API}/surveys/{survey_id}/progress")
        progress.raise_for_status()
        observed.add(progress.json()["stage"])
        response = await client.get(f"{API}/surveys/{survey_id}")
        response.raise_for_status()
        survey = response.json()
        if survey["status"] == expected_status:
            if not ({"waiting_for_execution", "finalizing", "completed"} & observed):
                raise AssertionError(f"no public progress stages were observed: {observed}")
            return survey
        if survey["status"] in {"succeeded", "failed", "cancelled"}:
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


async def _assert_database_and_archive(s3: Any, survey_id: str) -> tuple[str, str]:
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
        paths = {item["path"] for item in manifest["files"]}
        assert {
            "run/00_survey_spec.md",
            "run/01_query_plan.md",
            "run/02_candidate_pool.md",
            "run/03_expansion.md",
            "run/04_ranked_pool.md",
            "run/cards/2401.12345.md",
            "run/05_research_map.md",
            "run/06_judge_panel.md",
            "run/00_outline.md",
            "run/00_outline.json",
            "run/sections/01_introduction.md",
            "run/08_survey.md",
            "run/index.md",
            "run/trajectory.jsonl",
            "run/diagnostics.json",
            "run.json",
        }.issubset(paths)
        run_record = json.loads(
            _read_object(
                s3, next(item["key"] for item in manifest["files"] if item["path"] == "run.json")
            )
        )
        assert run_record["schema_version"] == 2
        assert run_record["process"]["return_code"] == 0
        assert run_record["process"]["termination_reason"] == "completed"
        assert run_record["diagnostics"]["last_successful_component"] in {
            "survey_outline",
            "survey_finalizer",
        }
        assert run_record["diagnostics"]["tool_counts"]["started"] > 0
        assert run_record["diagnostics"]["tool_counts"]["finished"] > 0
        assert run_record["diagnostics"]["tool_counts"]["failed"] == 0
        missing_errors = {
            anomaly["expected_artifact"]
            for anomaly in run_record["diagnostics"]["anomalies"]
            if anomaly["severity"] == "error"
        }
        assert "08_survey.md" not in missing_errors
        assert "index.md" not in missing_errors
        diagnostics_artifact = next(
            item for item in manifest["files"] if item["path"] == "run/diagnostics.json"
        )
        diagnostics_body = _read_object(s3, diagnostics_artifact["key"])
        assert hashlib.sha256(diagnostics_body).hexdigest() == diagnostics_artifact["sha256"]
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
        notify_on_completion = await connection.fetchval(
            "SELECT notify_on_completion FROM scholight.surveys WHERE id = $1::uuid",
            survey_id,
        )
        assert notify_on_completion is True
        notification = None
        for _ in range(30):
            notification = await connection.fetchrow(
                "SELECT survey_outcome, status, attempts "
                "FROM scholight.survey_email_notifications WHERE survey_id = $1::uuid",
                survey_id,
            )
            if notification is not None and notification["status"] == "succeeded":
                break
            await asyncio.sleep(0.25)
        assert notification is not None
        assert notification["survey_outcome"] == "succeeded"
        assert notification["status"] == "succeeded"
        assert notification["attempts"] == 1
        return str(job["id"]), str(job["manifest_key"])
    finally:
        await connection.close()


async def _assert_public_report_and_artifacts(
    client: httpx.AsyncClient,
    survey_id: str,
) -> None:
    listing = await client.get(f"{API}/surveys", params={"view": "all"})
    listing.raise_for_status()
    listed = next(item for item in listing.json()["items"] if item["id"] == survey_id)
    assert listed["report_available"] is True
    assert listed["artifacts_available"] is True
    assert listing.json()["quota"]["succeeded"] == 1

    report = await client.get(f"{API}/surveys/{survey_id}/report")
    report.raise_for_status()
    assert "This deterministic section is grounded" in report.text

    artifacts = await client.get(f"{API}/surveys/{survey_id}/artifacts")
    artifacts.raise_for_status()
    diagnostics_artifact = next(
        item for item in artifacts.json()["items"] if item["path"] == "run/diagnostics.json"
    )
    download = await client.get(diagnostics_artifact["download_url"])
    download.raise_for_status()
    assert hashlib.sha256(download.content).hexdigest() == diagnostics_artifact["sha256"]


async def _assert_real_graph_reaches_section_writers(client: httpx.AsyncClient) -> None:
    response = await client.get("http://model:8080/stats")
    response.raise_for_status()
    counts = response.json()["request_counts"]
    for component in (
        "anchor",
        "discovery_merger",
        "expansion_merger",
        "paper_card",
        "judge_synthesizer",
        "survey_outline",
        "section_expander",
    ):
        assert counts.get(component, 0) > 0
    assert counts.get("survey_assembler", 0) == 0


async def _assert_missing_candidate_pool_diagnostics(
    client: httpx.AsyncClient,
    s3: Any,
) -> tuple[str, str]:
    created = await client.post(
        f"{API}/surveys",
        json={
            "initial_request": "Exercise missing candidate-pool contract diagnostics",
            "client_request_id": str(uuid4()),
        },
    )
    created.raise_for_status()
    survey_id = created.json()["id"]
    first = await _poll_draft(client, survey_id, expected_revision=1)
    manual = await client.post(
        f"{API}/surveys/{survey_id}/drafts/manual",
        json={
            "markdown": first["markdown"] + "\n\nOMIT_E2E_CANDIDATE_POOL",
            "message": "Inject a completed merger with no primary artifact",
            "client_request_id": str(uuid4()),
        },
    )
    manual.raise_for_status()
    started = await client.post(
        f"{API}/surveys/{survey_id}/start",
        json={"client_request_id": str(uuid4())},
    )
    started.raise_for_status()
    await _poll_survey(client, survey_id, expected_status="failed")

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
        assert job["terminal_outcome"] == "failed"
        manifest = json.loads(_read_object(s3, job["manifest_key"]))
        paths = {item["path"] for item in manifest["files"]}
        assert "run/02_candidate_pool.md" not in paths
        assert "run/03_expansion.md" in paths
        assert "run/08_survey.md" in paths
        assert "run/index.md" in paths
        run_record = json.loads(
            _read_object(
                s3, next(item["key"] for item in manifest["files"] if item["path"] == "run.json")
            )
        )
        assert run_record["process"]["return_code"] == 0
        assert run_record["process"]["termination_reason"] == "contract_violation"
        assert run_record["diagnostics"]["first_anomaly"] == {
            "component": "discovery_merger",
            "expected_artifact": "02_candidate_pool.md",
            "kind": "required_artifact_missing",
            "severity": "error",
        }
        assert run_record["diagnostics"]["last_successful_component"] in {
            "survey_outline",
            "survey_finalizer",
        }
        assert run_record["diagnostics"]["affected_components"] == [
            "expansion",
            "rank_pool",
            "card_plan",
            "research_map",
            "judge_panel",
            "image_planner",
            "survey_outline",
            "survey_finalizer",
        ]
        return survey_id, str(job["manifest_key"])
    finally:
        await connection.close()


async def _poll_status(
    client: httpx.AsyncClient,
    survey_id: str,
    expected: str,
    *,
    attempts: int = 60,
    user_id: int = 42,
) -> dict[str, Any]:
    for _ in range(attempts):
        response = await client.get(
            f"{API}/surveys/{survey_id}",
            headers={"X-E2E-User-Id": str(user_id)},
        )
        response.raise_for_status()
        survey = response.json()
        if survey["status"] == expected:
            return survey
        await asyncio.sleep(1)
    raise AssertionError(f"Survey did not enter {expected}")


async def _queue_counts(table: str) -> tuple[int, int, int]:
    queries = {
        "survey_drafts": (
            "SELECT count(*) FILTER (WHERE q.status = 'running') AS running, "
            "count(*) FILTER (WHERE q.status = 'queued') AS queued, "
            "coalesce(max(per_user.running), 0) AS max_per_user "
            "FROM scholight.survey_drafts q "
            "JOIN scholight.surveys s ON s.id = q.survey_id "
            "LEFT JOIN LATERAL (SELECT count(*) AS running "
            "FROM scholight.survey_drafts own "
            "JOIN scholight.surveys os ON os.id = own.survey_id "
            "WHERE own.status = 'running' AND os.user_id = s.user_id) per_user ON true"
        ),
        "survey_jobs": (
            "SELECT count(*) FILTER (WHERE q.status = 'running') AS running, "
            "count(*) FILTER (WHERE q.status = 'queued') AS queued, "
            "coalesce(max(per_user.running), 0) AS max_per_user "
            "FROM scholight.survey_jobs q "
            "JOIN scholight.surveys s ON s.id = q.survey_id "
            "LEFT JOIN LATERAL (SELECT count(*) AS running "
            "FROM scholight.survey_jobs own "
            "JOIN scholight.surveys os ON os.id = own.survey_id "
            "WHERE own.status = 'running' AND os.user_id = s.user_id) per_user ON true"
        ),
    }
    if table not in queries:
        raise AssertionError("invalid E2E queue table")
    connection = await asyncpg.connect(
        host="postgres",
        port=5432,
        database="survey_e2e",
        user="postgres",
        password="survey-local-only",
    )
    try:
        row = await connection.fetchrow(queries[table])
        assert row is not None
        return int(row["running"]), int(row["queued"]), int(row["max_per_user"])
    finally:
        await connection.close()


async def _wait_for_queue_shape(
    *,
    table: str,
    running: int,
    queued: int,
    per_user_limit: int,
) -> None:
    for _ in range(120):
        actual_running, actual_queued, max_per_user = await _queue_counts(table)
        if actual_running == running and actual_queued == queued:
            assert max_per_user <= per_user_limit
            return
        await asyncio.sleep(0.25)
    raise AssertionError(
        f"{table} did not reach running={running}, queued={queued}; "
        f"last={actual_running}/{actual_queued}"
    )


async def _assert_bounded_fair_queues(client: httpx.AsyncClient) -> None:
    """Exercise the real supervisors at their configured per-worker limits."""

    async def _create(user_id: int, sequence: int) -> tuple[int, str]:
        response = await client.post(
            f"{API}/surveys",
            headers={"X-E2E-User-Id": str(user_id)},
            json={
                "initial_request": f"SLOW_E2E_DRAFT user {user_id} item {sequence}",
                "client_request_id": str(uuid4()),
            },
        )
        response.raise_for_status()
        return user_id, response.json()["id"]

    surveys = await asyncio.gather(
        *(_create(user_id, sequence) for user_id in range(100, 105) for sequence in range(2))
    )
    await _wait_for_queue_shape(
        table="survey_drafts",
        running=8,
        queued=2,
        per_user_limit=2,
    )
    queued_drafts: list[tuple[int, str]] = []
    for user_id, survey_id in surveys:
        response = await client.get(
            f"{API}/surveys/{survey_id}/progress",
            headers={"X-E2E-User-Id": str(user_id)},
        )
        response.raise_for_status()
        if response.json()["stage"] == "waiting_for_draft":
            queued_drafts.append((user_id, survey_id))
            assert response.json()["queue"]["kind"] == "draft"
            assert response.json()["queue"]["position"] >= 1
    assert len(queued_drafts) == 2

    await asyncio.gather(
        *(
            _poll_draft(client, survey_id, expected_revision=1, user_id=user_id)
            for user_id, survey_id in surveys
        )
    )

    formal = [next(item for item in surveys if item[0] == user_id) for user_id in range(100, 104)]
    for user_id, survey_id in formal:
        response = await client.post(
            f"{API}/surveys/{survey_id}/drafts/manual",
            headers={"X-E2E-User-Id": str(user_id)},
            json={
                "markdown": "# Bounded queue E2E\n\nSLOW_E2E_FORMAL",
                "message": "Hold the formal worker slot for queue verification",
                "client_request_id": str(uuid4()),
            },
        )
        response.raise_for_status()
    started = await asyncio.gather(
        *(
            client.post(
                f"{API}/surveys/{survey_id}/start",
                headers={"X-E2E-User-Id": str(user_id)},
                json={"client_request_id": str(uuid4())},
            )
            for user_id, survey_id in formal
        )
    )
    for response in started:
        response.raise_for_status()
    await _wait_for_queue_shape(
        table="survey_jobs",
        running=1,
        queued=3,
        per_user_limit=1,
    )
    queued_jobs: list[tuple[int, str]] = []
    for user_id, survey_id in formal:
        response = await client.get(
            f"{API}/surveys/{survey_id}/progress",
            headers={"X-E2E-User-Id": str(user_id)},
        )
        response.raise_for_status()
        if response.json()["stage"] == "waiting_for_execution":
            queued_jobs.append((user_id, survey_id))
            assert response.json()["queue"]["kind"] == "survey"
            assert response.json()["queue"]["position"] >= 1
    assert len(queued_jobs) == 3

    for user_id, survey_id in formal:
        response = await client.post(
            f"{API}/surveys/{survey_id}/cancel",
            headers={"X-E2E-User-Id": str(user_id)},
        )
        response.raise_for_status()
    await asyncio.gather(
        *(
            _poll_status(
                client,
                survey_id,
                "cancelled",
                attempts=90,
                user_id=user_id,
            )
            for user_id, survey_id in formal
        )
    )


async def _assert_running_cancel(client: httpx.AsyncClient) -> str:
    created = await client.post(
        f"{API}/surveys",
        json={
            "initial_request": "Exercise cooperative Survey cancellation",
            "client_request_id": str(uuid4()),
        },
    )
    created.raise_for_status()
    survey_id = created.json()["id"]
    first = await _poll_draft(client, survey_id, expected_revision=1)
    manual = await client.post(
        f"{API}/surveys/{survey_id}/drafts/manual",
        json={
            "markdown": first["markdown"] + "\n\nSLOW_E2E_CANCEL",
            "message": "Exercise cancellation while the formal workflow is running",
            "client_request_id": str(uuid4()),
        },
    )
    manual.raise_for_status()
    started = await client.post(
        f"{API}/surveys/{survey_id}/start",
        json={"client_request_id": str(uuid4())},
    )
    started.raise_for_status()
    await _poll_status(client, survey_id, "running")
    cancelled = await client.post(f"{API}/surveys/{survey_id}/cancel")
    cancelled.raise_for_status()
    assert cancelled.json()["status"] == "running"
    progress = await client.get(f"{API}/surveys/{survey_id}/progress")
    progress.raise_for_status()
    assert progress.json()["stage"] in {"cancelling", "cancelled"}
    await _poll_status(client, survey_id, "cancelled", attempts=90)
    artifacts = await client.get(f"{API}/surveys/{survey_id}/artifacts")
    artifacts.raise_for_status()
    assert artifacts.json()["items"]
    return survey_id


async def _delete_and_wait_for_cleanup(
    client: httpx.AsyncClient,
    s3: Any,
    survey_id: str,
    manifest_key: str,
) -> None:
    deleted = await client.delete(f"{API}/surveys/{survey_id}")
    assert deleted.status_code == 204
    missing = await client.get(f"{API}/surveys/{survey_id}")
    assert missing.status_code == 404
    prefix = manifest_key.removesuffix("/manifest.json")
    for _ in range(60):
        remaining = await asyncio.to_thread(
            s3.list_objects_v2,
            Bucket=BUCKET,
            Prefix=prefix,
        )
        if remaining.get("KeyCount", 0) == 0:
            return
        await asyncio.sleep(1)
    raise AssertionError("Survey artifact cleanup did not remove the deleted run")


async def main() -> None:
    # The bounded-fairness scenario intentionally issues concurrent requests.
    # Keep a finite deadline while allowing slower shared CI runners to drain them.
    timeout = httpx.Timeout(30.0)
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
        await _assert_bounded_fair_queues(client)
        created = await client.post(
            f"{API}/surveys",
            json={
                "initial_request": "Survey retrieval-augmented generation evaluation",
                "client_request_id": str(uuid4()),
            },
        )
        created.raise_for_status()
        assert created.json()["title"] == "Retrieval-Augmented Generation Evaluation"
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
            json={
                "client_request_id": str(uuid4()),
                "notify_on_completion": True,
            },
        )
        started.raise_for_status()
        assert started.json()["status"] == "queued"
        await _poll_survey(client, survey_id)
        _job_id, manifest_key = await _assert_database_and_archive(s3, survey_id)
        await _assert_public_report_and_artifacts(client, survey_id)
        (
            injected_survey_id,
            injected_manifest_key,
        ) = await _assert_missing_candidate_pool_diagnostics(
            client,
            s3,
        )
        await _assert_real_graph_reaches_section_writers(client)
        cancelled_survey_id = await _assert_running_cancel(client)
        cancelled_artifacts = await client.get(f"{API}/surveys/{cancelled_survey_id}/artifacts")
        cancelled_artifacts.raise_for_status()
        await _delete_and_wait_for_cleanup(client, s3, survey_id, manifest_key)
        await _delete_and_wait_for_cleanup(
            client,
            s3,
            injected_survey_id,
            injected_manifest_key,
        )
        logging.getLogger(__name__).info("survey e2e passed")


if __name__ == "__main__":
    asyncio.run(main())
