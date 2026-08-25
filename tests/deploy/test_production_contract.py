"""Static contracts for the active ECS production path."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
ECS = ROOT / "deploy" / "ecs"
FRONTEND_RUNTIME = ROOT / "frontend" / "runtime"


def test_local_extract_sidecar_is_isolated_and_resource_bounded() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    extract = services["extract"]

    assert "extract" not in services["api"].get("depends_on", {})
    assert set(services["api"]["networks"]) == {"scholight", "extract-control"}
    assert set(extract["networks"]) == {"extract-control", "extract-egress"}
    assert extract["mem_limit"] == "1536m"
    assert extract["memswap_limit"] == "1536m"
    assert str(extract["cpus"]) == "1.0"
    assert extract["pids_limit"] == 256
    assert compose["networks"]["extract-control"]["internal"] is True
    assert compose["networks"]["extract-egress"]["internal"] is False


def test_extract_image_does_not_install_identity_sdk() -> None:
    dockerfile = (ROOT / "docker/scholight-extract/Dockerfile").read_text(encoding="utf-8")

    assert "--no-install-package sanchezcloud-identity" in dockerfile
    assert "--no-install-package cloud-auth" not in dockerfile


def test_uvicorn_has_explicit_tunable_connection_boundaries() -> None:
    entrypoint = (ROOT / "docker/scholight-api/start.py").read_text(encoding="utf-8")

    assert "timeout_keep_alive=settings.server_keep_alive_seconds" in entrypoint
    assert "limit_concurrency=settings.server_limit_concurrency" in entrypoint
    assert "backlog=settings.server_backlog" in entrypoint


def test_source_contains_no_production_dependency_defaults_or_secret_fragments() -> None:
    config = (ROOT / "scholight/config.py").read_text(encoding="utf-8")
    store_client = (ROOT / "scholight/store/client.py").read_text(encoding="utf-8")
    search_engine = (ROOT / "scholight/search/engine.py").read_text(encoding="utf-8")

    assert "serverless.ali-cn-hangzhou.cloud.zilliz.com.cn" not in config
    assert "openapi-qb-nat.sii.edu.cn" not in config
    assert "cf0m0gegaz1c.ap-east-1.rds.amazonaws.com" not in config
    assert "token=masked" not in store_client
    assert "query=request.query[:80]" not in search_engine
    assert "query_length=len(request.query)" in search_engine


def test_frontend_agent_document_templates_use_canonical_url_placeholder() -> None:
    llms = (FRONTEND_RUNTIME / "llms.txt.template").read_text(encoding="utf-8")
    docs = (FRONTEND_RUNTIME / "docs.md.template").read_text(encoding="utf-8")
    robots = (ROOT / "frontend/public/robots.txt").read_text(encoding="utf-8")

    assert llms.startswith("# Scholight\n")
    assert "(@@SCHOLIGHT_PUBLIC_WEB_URL@@/docs.md)" in llms
    assert "(@@SCHOLIGHT_PUBLIC_WEB_URL@@/api/openapi.json)" in llms
    assert "(@@SCHOLIGHT_PUBLIC_WEB_URL@@/api/mcp)" in llms
    assert "export SCHOLIGHT_BASE_URL=@@SCHOLIGHT_PUBLIC_WEB_URL@@" in docs
    assert "@@SCHOLIGHT_PUBLIC_WEB_URL@@/api/search" in docs
    assert "@@SCHOLIGHT_PUBLIC_WEB_URL@@/api/mcp" in docs
    assert "@@SCHOLIGHT_PUBLIC_WEB_URL@@/api/openapi.json" in docs
    assert "User-agent: *" in robots
    assert "Allow: /" in robots


def test_frontend_renders_agent_documents_from_public_web_url(tmp_path: Path) -> None:
    output = tmp_path / "rendered"
    env = {**os.environ, "SCHOLIGHT_PUBLIC_WEB_URL": "https://scholight.sanchezcloud.net"}

    result = subprocess.run(
        ["sh", str(FRONTEND_RUNTIME / "render-docs.sh"), str(FRONTEND_RUNTIME), str(output)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    docs = (output / "docs.md").read_text(encoding="utf-8")
    llms = (output / "llms.txt").read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert "https://scholight.sanchezcloud.net/api/search" in docs
    assert "https://scholight.sanchezcloud.net/docs.md" in llms
    assert "@@SCHOLIGHT_PUBLIC_WEB_URL@@" not in docs + llms


def test_frontend_rejects_invalid_public_web_url(tmp_path: Path) -> None:
    output = tmp_path / "rendered"
    env = {**os.environ, "SCHOLIGHT_PUBLIC_WEB_URL": "https://example.com/forged/path"}

    result = subprocess.run(
        ["sh", str(FRONTEND_RUNTIME / "render-docs.sh"), str(FRONTEND_RUNTIME), str(output)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert not output.exists()


def test_frontend_serves_agent_documents_without_spa_fallback() -> None:
    nginx = (ROOT / "frontend/nginx.conf").read_text(encoding="utf-8")
    dockerfile = (ROOT / "frontend/Dockerfile").read_text(encoding="utf-8")

    for path in ("/llms.txt", "/.well-known/llms.txt", "/docs.md", "/robots.txt"):
        assert f"location = {path}" in nginx
    assert "default_type text/markdown;" in nginx
    assert "try_files /index.html =404;" in nginx
    assert "runtime/render-docs.sh" in dockerfile
    assert "/docker-entrypoint.d/40-render-scholight-docs.sh" in dockerfile


def test_frontend_joins_service_connect_as_an_api_client() -> None:
    template = (ECS / "scholight-production.yml").read_text(encoding="utf-8")
    web_service = template.split("  WebService:", maxsplit=1)[1].split("  ApiService:", maxsplit=1)[
        0
    ]

    assert "ServiceConnectConfiguration:" in web_service
    assert "Enabled: true" in web_service
    assert "Namespace: !ImportValue sanchezcloud-production-namespace-arn" in web_service
    assert "Services:" not in web_service


def test_active_workflows_are_oidc_manifest_and_digest_driven() -> None:
    publish = (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    database = (ROOT / ".github/workflows/database-production.yml").read_text(encoding="utf-8")

    assert "workflow_run:" in publish
    assert "environment: image-publish" in publish
    assert "id-token: write" in publish + release + database
    assert ":latest" not in publish
    assert "AWS_ACCESS_KEY_ID" not in publish + release + database
    assert "AWS_SECRET_ACCESS_KEY" not in publish + release + database
    assert "scripts/release_manifest.py verify" in release + database
    assert "aws cloudformation deploy" in release
    assert '--s3-bucket "$template_bucket"' in release
    assert '--s3-prefix "cloudformation/${RELEASE_SHA}"' in release
    assert "aws ecs wait services-stable" in release
    assert "aws ecs run-task" in database
    assert "AWS-RunShellScript" not in release + database
    assert "send-command" not in release + database
    assert 'default: "off"' in release
    assert 'options: ["off", "all"]' in release
    assert "MIGRATE SCHOLIGHT PRODUCTION" in database
    assert 'expected_confirmation="${OPERATION^^} SCHOLIGHT PRODUCTION"' in release


def test_release_runs_candidate_survey_canaries_before_deployment() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    canary = workflow.split("- name: Register candidate Survey canary task", maxsplit=1)[1]
    canary = canary.split("- name: Deploy digest-qualified ECS release", maxsplit=1)[0]
    assert "sanchezcloud-scholight-survey-canary" in canary
    assert "scholight survey model-canary --json-output" in canary
    assert "scholight survey image-canary --json-output" in canary
    assert "scholight survey fulltext-canary --json-output" in canary
    assert "aws ecs describe-tasks" in canary
    assert "deadline=$((SECONDS + 1200))" in canary
    assert "aws ecs deregister-task-definition" in canary
    assert '--task-definition "$CANARY_TASK_DEFINITION"' in canary


def test_production_survey_rerun_workflow_is_fixed_and_owner_preserving() -> None:
    workflow = (ROOT / ".github/workflows/survey-production-rerun.yml").read_text(encoding="utf-8")
    foundation = (ECS / "scholight-foundation.yml").read_text(encoding="utf-8")
    deploy_role = foundation.split("  ProductionDeployRole:", maxsplit=1)[1].split(
        "  DatabaseDeployRole:", maxsplit=1
    )[0]

    assert "environment: production" in workflow
    assert "id-token: write" in workflow
    assert "role-duration-seconds: 14400" in workflow
    assert "MaxSessionDuration: 14400" in deploy_role
    assert "RERUN SCHOLIGHT SURVEY" in workflow
    assert '"python","-m","scholight.survey.production_ops"' in workflow
    assert "rerun-and-verify" in workflow
    assert "--source-survey-id" in workflow
    assert "--operation-id" in workflow
    assert '"--minimum-coverage","80"' in workflow
    assert "--notify-on-completion" in workflow
    assert "aws ecs run-task" in workflow
    assert "aws ecs describe-tasks" in workflow
    assert "status=$(jq -r '.tasks[0].lastStatus'" in workflow
    assert "inputs.command" not in workflow


def test_production_survey_evidence_repair_is_fixed_guarded_and_serialized() -> None:
    workflow = (ROOT / ".github/workflows/survey-production-evidence-repair.yml").read_text(
        encoding="utf-8"
    )

    assert "environment: production" in workflow
    assert "group: scholight-production" in workflow
    assert "VERIFY SURVEY EVIDENCE REPAIR" in workflow
    assert "APPLY SURVEY EVIDENCE REPAIR" in workflow
    assert "f4795522-28f6-4edd-8813-102f654d4367" in workflow
    assert "d4568b259f1fd0c89e9be975a32b8938a9839b9ca64cdc1c848bf6a6613f31e0" in workflow
    assert "34a1ae81bcbb93c518cdc5d9ca52bb84a76a9f452447aa8a221df5e89eae8984" in workflow
    assert "repair-degraded-evidence" in workflow
    assert "run_repair_task verify" in workflow
    assert "run_repair_task apply" in workflow
    assert 'if $mode == "apply" then ["--apply"]' in workflow
    assert "sanchezcloud-scholight-survey-canary" in workflow
    assert "deployed_api_image" in workflow
    assert "deployed_survey_image" in workflow
    assert "aws ecs run-task" in workflow
    assert "aws ecs deregister-task-definition" in workflow
    assert "inputs.command" not in workflow
    assert "source_survey_id:" not in workflow
    assert "job_id:" not in workflow


def test_survey_capacity_contract_is_explicit_and_staged() -> None:
    runtime = (ECS / "scholight-production.yml").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    example = yaml.safe_load(
        (ECS / "production.parameters.example.json").read_text(encoding="utf-8")
    )
    draft_task = runtime.split("  SurveyDraftTaskDefinition:", maxsplit=1)[1].split(
        "  SurveyTaskDefinition:", maxsplit=1
    )[0]
    full_task = runtime.split("  SurveyTaskDefinition:", maxsplit=1)[1].split(
        "  MigrationTaskDefinition:", maxsplit=1
    )[0]

    assert 'SCHOLIGHT_SURVEY_DRAFT_GLOBAL_CONCURRENCY, Value: "64"' in draft_task
    assert 'SCHOLIGHT_SURVEY_DRAFT_PER_USER_CONCURRENCY, Value: "8"' in draft_task
    assert 'SCHOLIGHT_SURVEY_DRAFT_WORKER_CONCURRENCY, Value: "8"' in draft_task
    assert 'SCHOLIGHT_SURVEY_MCP_URL, Value: !Sub "https://${DomainName}/api/mcp"' in draft_task
    assert 'SCHOLIGHT_PG_POOL_MIN_SIZE, Value: "1"' in draft_task
    assert 'SCHOLIGHT_PG_POOL_MAX_SIZE, Value: "4"' in draft_task
    assert 'SCHOLIGHT_SURVEY_JOB_GLOBAL_CONCURRENCY, Value: "16"' in full_task
    assert 'SCHOLIGHT_SURVEY_JOB_PER_USER_CONCURRENCY, Value: "4"' in full_task
    assert 'Cpu: "1024"' in full_task
    assert 'Memory: "2048"' in full_task
    assert "EphemeralStorage:" not in full_task
    assert 'SCHOLIGHT_SURVEY_JOB_WORKER_CONCURRENCY, Value: "2"' in full_task
    assert 'SCHOLIGHT_SURVEY_MCP_URL, Value: !Sub "https://${DomainName}/api/mcp"' in full_task
    assert 'SCHOLIGHT_PG_POOL_MIN_SIZE, Value: "1"' in full_task
    assert 'SCHOLIGHT_PG_POOL_MAX_SIZE, Value: "2"' in full_task
    assert "IMAGE_GEN_API_URL, Value: !Ref ImageGenApiUrl" in full_task
    assert "IMAGE_GEN_TRUSTED_HOSTS, Value: !Ref ImageGenTrustedHosts" in full_task
    assert example["ImageGenApiUrl"] == ""
    assert example["ImageGenTrustedHosts"] == ""
    assert re.search(r"Name: SCHOLIGHT_SURVEY_DRAFT_CONCURRENCY(?:,|\s*})", runtime) is None
    assert re.search(r"Name: SCHOLIGHT_SURVEY_JOB_CONCURRENCY(?:,|\s*})", runtime) is None

    assert "SurveyDraftMaxTasks:" in runtime
    assert "MaxValue: 8" in runtime
    assert "SurveyFullMaxTasks:" in runtime
    assert (
        runtime.split("  SurveyFullMaxTasks:", maxsplit=1)[1]
        .split("\n\nRules:", maxsplit=1)[0]
        .count("MaxValue: 8")
        == 1
    )
    assert example["SurveyDraftMaxTasks"] == 1
    assert example["SurveyFullMaxTasks"] == 1
    assert "options: [1/1, 2/2, 4/4, 8/8]" in workflow
    assert "1/1|2/2|4/4|8/8" in workflow
    assert "8/16" not in workflow
    assert "scripts/check_survey_capacity_stage.py" in workflow


def test_survey_worker_autoscaling_and_protection_are_bounded() -> None:
    runtime = (ECS / "scholight-production.yml").read_text(encoding="utf-8")
    foundation = (ECS / "scholight-foundation.yml").read_text(encoding="utf-8")

    draft_scaling = runtime.split("  SurveyDraftBacklogScalingPolicy:", maxsplit=1)[1].split(
        "  SurveyFullScalableTarget:", maxsplit=1
    )[0]
    full_scaling = runtime.split("  SurveyFullBacklogScalingPolicy:", maxsplit=1)[1].split(
        "  IngestSchedule:", maxsplit=1
    )[0]
    assert "TargetValue: 8" in draft_scaling
    assert "SurveyDraftOutstanding" in draft_scaling
    assert "TargetValue: 2" in full_scaling
    assert "SurveyJobOutstanding" in full_scaling
    for policy in (draft_scaling, full_scaling):
        assert "ScaleOutCooldown: 60" in policy
        assert "ScaleInCooldown: 900" in policy
        assert "RunningTaskCount" in policy
        assert "backlog_per_task" in policy

    survey_role = runtime.split("  SurveyTaskRole:", maxsplit=1)[1].split(
        "  SchedulerRole:", maxsplit=1
    )[0]
    assert "ecs:GetTaskProtection" in survey_role
    assert "ecs:UpdateTaskProtection" in survey_role
    deploy_role = foundation.split("  ProductionDeployRole:", maxsplit=1)[1].split(
        "  DatabaseDeployRole:", maxsplit=1
    )[0]
    assert "cloudwatch:GetMetricStatistics" in deploy_role
    assert "rds:DescribeDBInstances" not in deploy_role
    assert "servicequotas:GetServiceQuota" in deploy_role


def test_survey_capacity_observability_has_no_identifier_dimensions() -> None:
    runtime = (ECS / "scholight-production.yml").read_text(encoding="utf-8")

    for metric in (
        "SurveyDraftOldestQueuedAge",
        "SurveyJobOldestQueuedAge",
        "SurveyDraftUsersAtConcurrencyLimit",
        "SurveyJobUsersAtConcurrencyLimit",
        "SurveyDraftClaimLatency",
        "SurveyDraftHeartbeatLatency",
        "SurveyJobClaimLatency",
        "SurveyJobHeartbeatLatency",
        "SurveyTaskProtectionFailure",
        "SurveyProviderThrottled",
        "SurveyFinalizationFailure",
        "SurveyModelTerminalFailure",
        "SurveyModelCanaryCount",
        "SurveyPaperEvidenceCount",
        "SurveyFullTextCoverage",
        "SurveyFullTextRuntimeFailure",
        "SurveyFullTextCanaryCount",
        "SurveyImageGenerationCount",
    ):
        assert metric in runtime
    assert "SurveyDraftProviderThrottleAlarm:" in runtime
    assert "SurveyFullProviderThrottleAlarm:" in runtime
    assert "SurveyFinalizationFailureAlarm:" in runtime
    assert "SurveyModelTerminalFailureAlarm:" in runtime
    assert "SurveyFullTextRuntimeFailureAlarm:" in runtime
    assert "SurveyFullTextCanaryFailureAlarm:" in runtime
    assert "SurveyImageGenerationFailureAlarm:" in runtime
    assert "IF(jobs>0,100*throttled/jobs,0)" in runtime
    assert runtime.count("Threshold: 100") >= 2
    assert "MetricName: CPUUtilization" in runtime
    assert "MetricName: FreeableMemory" in runtime
    assert "Threshold: 104857600" in runtime
    assert "MetricName: SwapUsage" in runtime
    assert "Threshold: 67108864" in runtime
    assert "freeable memory above 500 MiB" in (ECS / "README.md").read_text(encoding="utf-8")
    assert "SurveyDraftNoTasksAlarm:" in runtime
    assert "SurveyFullNoTasksAlarm:" in runtime
    assert '"title":"Full Survey compute"' in runtime
    assert "EphemeralStorageUtilized" in runtime
    assert "EphemeralStorageReserved" in runtime
    dashboard = runtime.split("  Dashboard:", maxsplit=1)[1]
    for forbidden in ("user_id", "survey_id", "topic", "document"):
        assert forbidden not in dashboard


def test_release_defers_active_worker_images_and_gates_final_capacity() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "scripts/check_survey_worker_update.py" in workflow
    assert "SurveyDraftRunning" in workflow
    assert "SurveyJobRunning" in workflow
    assert "SURVEY WORKERS IDLE" in workflow
    assert "survey_capacity_prerequisites_confirmed" in workflow
    assert "L-3032A538" in workflow
    assert "inputs.survey_capacity_stage == '8/8'" in workflow
    assert "db.t4g.small or larger" not in workflow


def test_large_runtime_template_uses_bounded_s3_staging_permissions() -> None:
    foundation = (ECS / "scholight-foundation.yml").read_text(encoding="utf-8")
    runtime = (ECS / "scholight-production.yml").read_bytes()

    assert len(runtime) > 51_200
    assert "ExpireCloudFormationUploadArtifacts" in foundation
    assert "${ReleaseManifestBucket.Arn}/cloudformation/*" in foundation
    assert "Action: [s3:GetObject, s3:PutObject]" in foundation
    assert "Action: [kms:Decrypt, kms:GenerateDataKey]" in foundation


def test_image_publish_role_can_verify_pushed_manifests_and_attestations() -> None:
    foundation = (ECS / "scholight-foundation.yml").read_text(encoding="utf-8")
    publish_policy = foundation.split("PublishRole:", maxsplit=1)[1].split(
        "CloudFormationServiceRole:", maxsplit=1
    )[0]

    for action in (
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:CompleteLayerUpload",
        "ecr:GetDownloadUrlForLayer",
        "ecr:InitiateLayerUpload",
        "ecr:PutImage",
        "ecr:UploadLayerPart",
    ):
        assert f"- {action}" in publish_policy

    assert "ecr:CreateRepository" not in publish_policy
    assert "ecr:DeleteRepository" not in publish_policy
    assert "ecr:DeleteRepositoryPolicy" not in publish_policy


def test_cloudformation_role_can_manage_scoped_task_role_policies() -> None:
    foundation = (ECS / "scholight-foundation.yml").read_text(encoding="utf-8")
    cloudformation_policy = foundation.split("CloudFormationServiceRole:", maxsplit=1)[1].split(
        "ProductionDeployRole:", maxsplit=1
    )[0]

    assert "- iam:AttachRolePolicy" in cloudformation_policy
    assert "- iam:DetachRolePolicy" in cloudformation_policy
    assert (
        "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/SanchezCloudScholight*"
        in cloudformation_policy
    )
    assert "iam:AttachUserPolicy" not in cloudformation_policy
    assert "iam:AttachGroupPolicy" not in cloudformation_policy


def test_production_deploy_role_can_wait_for_scholight_services() -> None:
    foundation = (ECS / "scholight-foundation.yml").read_text(encoding="utf-8")
    deploy_policy = foundation.split("ProductionDeployRole:", maxsplit=1)[1].split(
        "DatabaseDeployRole:", maxsplit=1
    )[0]

    assert "Action: ecs:DescribeServices" in deploy_policy
    assert (
        "arn:${AWS::Partition}:ecs:${AWS::Region}:${AWS::AccountId}:service/"
        "sanchezcloud-production/scholight-*" in deploy_policy
    )
    assert "Action: ecs:*" not in deploy_policy


def test_production_deploy_role_can_run_candidate_survey_canaries() -> None:
    foundation = (ECS / "scholight-foundation.yml").read_text(encoding="utf-8")
    deploy_policy = foundation.split("ProductionDeployRole:", maxsplit=1)[1].split(
        "DatabaseDeployRole:", maxsplit=1
    )[0]

    for action in (
        "ecs:DeregisterTaskDefinition",
        "ecs:DescribeTaskDefinition",
        "ecs:DescribeTasks",
        "ecs:RegisterTaskDefinition",
        "ecs:RunTask",
        "ecs:StopTask",
    ):
        assert f"- {action}" in deploy_policy
    assert "task-definition/sanchezcloud-scholight-survey-canary:*" in deploy_policy
    assert "role/SanchezCloudScholightTaskExecutionRole" in deploy_policy
    assert "role/SanchezCloudScholightSurveyTaskRole" in deploy_policy
    assert "Action: [logs:GetLogEvents, logs:FilterLogEvents]" in deploy_policy
    assert "log-group:/sanchezcloud/scholight/survey:*" in deploy_policy
    assert "Action: ecs:*" not in deploy_policy


def test_production_deploy_role_can_run_owner_preserving_survey_reruns() -> None:
    foundation = (ECS / "scholight-foundation.yml").read_text(encoding="utf-8")
    deploy_policy = foundation.split("ProductionDeployRole:", maxsplit=1)[1].split(
        "DatabaseDeployRole:", maxsplit=1
    )[0]

    assert "task-definition/sanchezcloud-scholight-api:*" in deploy_policy
    assert "role/SanchezCloudScholightApiTaskRole" in deploy_policy
    assert "log-group:/sanchezcloud/scholight/api:*" in deploy_policy
    assert "cluster/sanchezcloud-production" in deploy_policy
    assert "Action: ecs:*" not in deploy_policy


def test_active_workflows_do_not_depend_on_frozen_ec2_package() -> None:
    active = "\n".join(
        (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
        for name in (
            "ci.yml",
            "database-production.yml",
            "publish.yml",
            "release.yml",
            "sanchezcloud-identity-compat.yml",
        )
    )

    assert "deploy/production" not in active
    assert "deploy/ecs/database-bootstrap.sql" in active


def test_frozen_ec2_package_cannot_host_the_unreleased_survey() -> None:
    legacy = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "deploy/production").iterdir()
        if path.is_file()
    ).lower()

    for fragment in (
        "survey-worker",
        "survey-draft-worker",
        "scholight_survey_runtime_enabled",
        "scholight_survey_public_mode",
        "surveyjobcount",
        "surveys/v1",
    ):
        assert fragment not in legacy


def test_first_ecs_deployment_can_bootstrap_without_starting_services() -> None:
    template = (ECS / "scholight-production.yml").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "ApplicationEnabled:" in template
    assert 'RunApplication: !Equals [!Ref ApplicationEnabled, "true"]' in template
    assert template.count("DesiredCount: !If [RunApplication, 1, 0]") == 3
    assert "MinCapacity: 1" in template
    assert template.count("State: !If [RunApplication, !Ref SchedulerState, DISABLED]") == 2
    for resource in (
        "ApiScalableTarget",
        "ApiCpuScalingPolicy",
        "ApiMemoryScalingPolicy",
        "ExtractScalableTarget",
        "ExtractCpuScalingPolicy",
        "ExtractMemoryScalingPolicy",
        "ApiUnhealthyAlarm",
        "ApiFiveHundredAlarm",
        "ApiLatencyAlarm",
        "ExtractFailureAlarm",
        "IngestionBacklogAgeAlarm",
        "IngestionDeadAlarm",
        "DatabaseConnectionPressureAlarm",
    ):
        assert re.search(
            rf"(?m)^  {resource}:\n    Type: .+\n    Condition: RunApplication$",
            template,
        )
    assert "if: inputs.application_enabled == 'true'" in workflow


def test_web_and_api_autoscaling_use_compute_and_alb_request_targets() -> None:
    runtime = (ECS / "scholight-production.yml").read_text(encoding="utf-8")

    web_target = runtime.split("  WebScalableTarget:", maxsplit=1)[1].split(
        "  WebCpuScalingPolicy:", maxsplit=1
    )[0]
    web_cpu = runtime.split("  WebCpuScalingPolicy:", maxsplit=1)[1].split(
        "  WebRequestScalingPolicy:", maxsplit=1
    )[0]
    web_requests = runtime.split("  WebRequestScalingPolicy:", maxsplit=1)[1].split(
        "  ApiScalableTarget:", maxsplit=1
    )[0]
    api_requests = runtime.split("  ApiRequestScalingPolicy:", maxsplit=1)[1].split(
        "  ExtractScalableTarget:", maxsplit=1
    )[0]

    assert "MinCapacity: 1" in web_target
    assert "MaxCapacity: 3" in web_target
    assert "TargetValue: 60" in web_cpu
    assert "ECSServiceAverageCPUUtilization" in web_cpu
    assert "TargetValue: 600" in web_requests
    assert "ALBRequestCountPerTarget" in web_requests
    assert "WebTargetGroup.TargetGroupFullName" in web_requests
    assert "TargetValue: 300" in api_requests
    assert "ALBRequestCountPerTarget" in api_requests
    assert "ApiTargetGroup.TargetGroupFullName" in api_requests


def test_all_long_running_services_roll_back_failed_deployments() -> None:
    runtime = (ECS / "scholight-production.yml").read_text(encoding="utf-8")
    service_names = ("Web", "Api", "Extract", "SurveyDraft", "Survey")

    for index, service_name in enumerate(service_names):
        start = f"  {service_name}Service:"
        end = (
            f"  {service_names[index + 1]}Service:"
            if index + 1 < len(service_names)
            else "  WebScalableTarget:"
        )
        service = runtime.split(start, maxsplit=1)[1].split(end, maxsplit=1)[0]
        assert "DeploymentCircuitBreaker:" in service
        assert "Enable: true" in service
        assert "Rollback: true" in service


def test_shared_database_alarms_cover_memory_swap_and_connections() -> None:
    runtime = (ECS / "scholight-production.yml").read_text(encoding="utf-8")
    memory = runtime.split("  DatabaseFreeableMemoryAlarm:", maxsplit=1)[1].split(
        "  DatabaseSwapUsageAlarm:", maxsplit=1
    )[0]
    swap = runtime.split("  DatabaseSwapUsageAlarm:", maxsplit=1)[1].split(
        "  SurveyRuntimeFailureAlarm:", maxsplit=1
    )[0]
    connections = runtime.split("  DatabaseConnectionPressureAlarm:", maxsplit=1)[1].split(
        "  DatabaseCpuAlarm:", maxsplit=1
    )[0]

    assert "Threshold: 104857600" in memory
    assert "EvaluationPeriods: 3" in memory
    assert "Period: 300" in memory
    assert "MetricName: SwapUsage" in swap
    assert "Threshold: 67108864" in swap
    assert "EvaluationPeriods: 3" in swap
    assert "Period: 300" in swap
    assert "MetricName: DatabaseConnections" in connections
    assert "Threshold: 60" in connections


def test_first_cutover_creates_the_dormant_stack_before_running_migrations() -> None:
    runbook = (ECS / "README.md").read_text(encoding="utf-8")
    cutover = runbook.split("## First cutover", maxsplit=1)[1].split(
        "## Failure handling", maxsplit=1
    )[0]

    assert cutover.count("protected product migration") == 1
    assert cutover.index("ApplicationEnabled=false") < cutover.index("protected product migration")
    assert cutover.index("protected product migration") < cutover.index("ApplicationEnabled=true")


def test_survey_public_mode_remains_a_string_in_cloudformation_rules() -> None:
    runtime = (ECS / "scholight-production.yml").read_text(encoding="utf-8")

    assert 'Default: "off"' in runtime
    assert 'AllowedValues: ["off", "all"]' in runtime
    assert '!Equals [!Ref SurveyPublicMode, "off"]' in runtime


def test_github_oidc_trust_uses_immutable_repository_identity() -> None:
    foundation = (ECS / "scholight-foundation.yml").read_text(encoding="utf-8")
    example = (ECS / "foundation.parameters.example.json").read_text(encoding="utf-8")

    immutable_prefix = "repo:EricSanchezok@115814526/scholight@1295347074"
    assert f"Default: {immutable_prefix}" in foundation
    assert immutable_prefix in example
    assert 'AllowedPattern: "^repo:[A-Za-z0-9_.-]+@[0-9]+/[A-Za-z0-9_.-]+@[0-9]+$"' in foundation
    for environment in ("image-publish", "database-production", "production"):
        assert f"${{GitHubOidcSubjectPrefix}}:environment:{environment}" in foundation


def test_database_workflow_can_pass_only_exact_migration_execution_role() -> None:
    foundation = (ECS / "scholight-foundation.yml").read_text(encoding="utf-8")
    database_policy = foundation.split("DatabaseDeployRole:", maxsplit=1)[1]

    assert "role/SanchezCloudScholightTaskExecutionRole" in database_policy
    assert "role/SanchezCloudScholight*" not in database_policy


def test_migration_task_pins_the_image_migration_directory() -> None:
    production = (ECS / "scholight-production.yml").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/database-production.yml").read_text(encoding="utf-8")
    migration_task = production.split("  MigrationTaskDefinition:", maxsplit=1)[1].split(
        "  WebService:", maxsplit=1
    )[0]

    assert 'Name: SCHOLIGHT_MIGRATIONS_DIR, Value: "/app/migrations"' in migration_task
    assert 'select(.name != "SCHOLIGHT_MIGRATIONS_DIR")' in workflow
    assert 'name: "SCHOLIGHT_MIGRATIONS_DIR", value: "/app/migrations"' in workflow


def test_survey_has_one_clean_initial_schema_without_compatibility_migrations() -> None:
    migration_names = {path.name for path in (ROOT / "migrations").glob("*.sql")}
    survey = (ROOT / "migrations/005_survey.sql").read_text(encoding="utf-8").lower()

    assert "005_survey.sql" in migration_names
    assert not any(
        fragment in name
        for name in migration_names
        for fragment in (
            "survey_jobs",
            "survey_aggregate",
            "survey_reliability",
            "survey_cancellation",
            "survey_titles",
            "survey_email_notifications",
        )
    )
    assert "drop table" not in survey
    assert "raise exception" not in survey
    assert "legacy" not in survey
    assert "survey_quota_overrides" not in migration_names
    assert "strength in ('standard', 'thorough', 'survey')" in survey


def test_python_images_have_explicit_minimal_runtime_targets() -> None:
    dockerfile = (ROOT / "docker/scholight-api/Dockerfile").read_text(encoding="utf-8")
    extract = (ROOT / "docker/scholight-extract/Dockerfile").read_text(encoding="utf-8")
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    for extra in ("api", "extract", "ingest", "survey"):
        assert f"{extra} = [" in project
    assert "FROM runtime-base AS api" in dockerfile
    assert "FROM runtime-base AS ingest" in dockerfile
    assert "FROM runtime-base AS survey" in dockerfile
    assert "--extra api" in dockerfile
    assert "--extra ingest" in dockerfile
    assert "--extra survey" in dockerfile
    assert "--extra extract" in extract
    assert dockerfile.count("github_token") == 2
    assert "FROM api AS final" not in dockerfile
    assert "/opt/scholight-package" not in dockerfile
    assert dockerfile.index("poppler-utils") > dockerfile.index("FROM runtime-base AS ingest")
    assert dockerfile.index("poppler-utils") < dockerfile.index("FROM runtime-base AS survey")


def test_api_image_smoke_imports_the_public_application() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    api_build = workflow.split("- name: Build API image", maxsplit=1)[1].split(
        "- name: Build ingest image", maxsplit=1
    )[0]

    assert "if: github.event_name != 'pull_request'" not in api_build
    assert 'find_spec("markdownify") is None' in api_build
    assert 'find_spec("playwright") is None' in api_build
    assert "import scholight.api.app; import scholight.api.extract_execution" in api_build


def test_survey_image_pins_verified_rcm_release() -> None:
    dockerfile = (ROOT / "docker/scholight-api/Dockerfile").read_text(encoding="utf-8")
    worker = (ROOT / "scholight/survey/worker.py").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "ARG RCM_VERSION=v0.2.19" in dockerfile
    assert 'RCM_VERSION = "0.2.19"' in worker
    assert "afe5c1c6fb7ad6842543faffa418c19ce8b145a2c955b93cd65e5ecd80a688f1" in dockerfile
    assert "sha256sum --check" in dockerfile
    assert "COPY --from=survey-builder /app/bin/accelerate /usr/local/bin/accelerate" in dockerfile
    assert "/releases/latest/" not in dockerfile
    assert "test -x /usr/local/bin/accelerate" in workflow
    assert '"accelerate 0.2.19"' in workflow


def test_pull_request_ci_builds_and_executes_survey_fulltext_image() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    build_step = workflow.split("- name: Build Survey image", maxsplit=1)[1].split(
        "- name:", maxsplit=1
    )[0]
    verify_step = workflow.split("- name: Verify Survey full-text image boundary", maxsplit=1)[
        1
    ].split("- name:", maxsplit=1)[0]

    assert "if:" not in build_step
    assert "target: survey" in build_step
    assert "if:" not in verify_step
    assert "command -v pdftotext" in verify_step
    assert "pdftotext /tmp/survey.pdf /tmp/survey.txt" in verify_step


def test_external_actions_are_pinned_to_full_commit_shas() -> None:
    action_reference = re.compile(r"^\s*uses:\s*([^\s]+)@([^\s#]+)", re.MULTILINE)
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        for action, revision in action_reference.findall(path.read_text(encoding="utf-8")):
            if action.startswith("./"):
                continue
            assert re.fullmatch(r"[0-9a-f]{40}", revision), f"{action}@{revision} is mutable"


def test_database_bootstrap_is_reviewed_and_ci_exercises_least_privilege_roles() -> None:
    bootstrap = (ECS / "database-bootstrap.sql").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "ALTER DEFAULT PRIVILEGES" in bootstrap
    assert "CREATE SCHEMA IF NOT EXISTS auth AUTHORIZATION %I" in bootstrap
    assert "CREATE SCHEMA IF NOT EXISTS scholight AUTHORIZATION %I" in bootstrap
    assert "GRANT REFERENCES ON TABLE auth.users" in bootstrap
    assert "GRANT SELECT, INSERT, UPDATE ON TABLE auth.users" in bootstrap
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE auth.users" not in bootstrap
    assert "GRANT SELECT ON TABLE auth.user_avatars" in bootstrap
    assert "INSERT, UPDATE, DELETE ON TABLE auth.user_avatars" not in bootstrap
    assert "REVOKE ALL ON TABLE auth.schema_migrations" in bootstrap
    assert "REVOKE ALL ON TABLE scholight.schema_migrations" in bootstrap
    assert "deploy/ecs/database-bootstrap.sql" in workflow
    assert "SCHOLIGHT_PG_USER=scholight_migrator" in workflow
    assert "-U scholight_app" in workflow
    assert "CREATE TABLE public.app_role_must_not_create" in workflow


def test_api_can_only_read_shared_profile_avatars() -> None:
    production = (ECS / "scholight-production.yml").read_text(encoding="utf-8")
    avatar_policy = production.split("PolicyName: ReadSharedProfileAvatars", maxsplit=1)[1].split(
        "SurveyTaskRole:", maxsplit=1
    )[0]

    assert "Action: s3:GetObject" in avatar_policy
    assert "s3:PutObject" not in avatar_policy
    assert "s3:DeleteObject" not in avatar_policy
    assert "Action: kms:Decrypt" in avatar_policy
    assert "Action: kms:Encrypt" not in avatar_policy
    assert "SCHOLIGHT_AVATAR_S3_BUCKET" in production


def test_runtime_secret_documents_cover_every_external_provider() -> None:
    foundation = (ECS / "scholight-foundation.yml").read_text(encoding="utf-8")
    production = (ECS / "scholight-production.yml").read_text(encoding="utf-8")

    assert "GenerateStringKey: auth_jwt_secret" in foundation
    delegation_secret = foundation.split("McpDelegationSecret:", maxsplit=1)[1].split(
        "SearchProvidersSecret:", maxsplit=1
    )[0]
    assert "DeletionPolicy: Retain" in delegation_secret
    assert "UpdateReplacePolicy: Retain" in delegation_secret
    assert "GenerateStringKey: mcp_delegation_jwt_secret" in delegation_secret
    assert "KmsKeyId: !GetAtt ConfigurationKey.Arn" in delegation_secret
    assert "sanchezcloud-scholight-mcp-delegation-secret-arn" in foundation
    assert "sanchezcloud-scholight-configuration-key-arn" in foundation
    survey_secret = foundation.split("SurveyProvidersSecret:", maxsplit=1)[1].split(
        "MailProvidersSecret:", maxsplit=1
    )[0]
    mail_secret = foundation.split("MailProvidersSecret:", maxsplit=1)[1].split(
        "AlertTopic:", maxsplit=1
    )[0]
    assert "aliyun_access_key_id" not in survey_secret
    assert "/sanchezcloud/scholight/production/mail" in mail_secret
    assert "sanchezcloud-scholight-mail-secret-arn" in foundation
    for key in (
        "anonymous_quota_hmac_secret",
        "access_key_hmac_secret",
        "mcp_delegation_jwt_secret",
        "extract_internal_token",
        "survey_mcp_jwt_secret",
        "zilliz_uri",
        "zilliz_token",
        "embedding_base_url",
        "embedding_api_key",
        "embedding_model",
        "mineru_api_key",
        "deepseek_api_key",
        "image_gen_api_key",
        "aliyun_access_key_id",
        "aliyun_access_key_secret",
        "aliyun_account_name",
    ):
        assert f'"{key}"' in foundation

    for variable in (
        "SCHOLIGHT_ALIYUN_DM_ACCESS_KEY_ID",
        "SCHOLIGHT_ALIYUN_DM_ACCESS_KEY_SECRET",
        "SCHOLIGHT_ALIYUN_DM_ACCOUNT_NAME",
    ):
        line = next(line for line in production.splitlines() if variable in line)
        assert "sanchezcloud-scholight-mail-secret-arn" in line

    for variable in (
        "SCHOLIGHT_ALIYUN_DM_ACCESS_KEY_ID",
        "SCHOLIGHT_ALIYUN_DM_ACCESS_KEY_SECRET",
        "SCHOLIGHT_ALIYUN_DM_ACCOUNT_NAME",
        "SCHOLIGHT_MINERU_API_KEY",
    ):
        assert variable in production


def test_api_injects_mcp_delegation_from_independent_retained_secret() -> None:
    foundation = (ECS / "scholight-foundation.yml").read_text(encoding="utf-8")
    production = (ECS / "scholight-production.yml").read_text(encoding="utf-8")
    execution_policy = production.split("PolicyName: ReadScholightRuntimeSecrets", maxsplit=1)[
        1
    ].split("ApiTaskRole:", maxsplit=1)[0]
    delegation_line = next(
        line for line in production.splitlines() if "SCHOLIGHT_MCP_DELEGATION_JWT_SECRET" in line
    )

    assert "sanchezcloud-scholight-mcp-delegation-secret-arn" in execution_policy
    assert "sanchezcloud-scholight-mcp-delegation-secret-arn" in delegation_line
    assert "sanchezcloud-scholight-core-secret-arn" not in delegation_line
    assert "Export: { Name: sanchezcloud-scholight-mcp-delegation-secret-arn }" in foundation
