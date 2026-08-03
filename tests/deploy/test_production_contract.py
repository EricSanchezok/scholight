"""Static contracts for the production deployment package."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
PRODUCTION = ROOT / "deploy" / "production"
FRONTEND_RUNTIME = ROOT / "frontend" / "runtime"
DIGEST_REFERENCE = re.compile(r"^[^\s:@]+(?:/[^\s:@]+)+@sha256:[0-9a-f]{64}$")


class _CloudFormationLoader(yaml.SafeLoader):
    """Load CloudFormation structure while preserving intrinsic function values."""


def _cloudformation_scalar(
    loader: _CloudFormationLoader,
    node: yaml.ScalarNode,
) -> dict[str, str]:
    return {node.tag.removeprefix("!"): loader.construct_scalar(node)}


for _tag in ("!Ref", "!Sub"):
    _CloudFormationLoader.add_constructor(_tag, _cloudformation_scalar)


def _load_observability_template() -> dict[str, object]:
    loaded = yaml.load(
        (PRODUCTION / "observability.yaml").read_text(encoding="utf-8"),
        Loader=_CloudFormationLoader,
    )
    assert isinstance(loaded, dict)
    return loaded


def test_production_compose_uses_digest_images_and_only_caddy_ports() -> None:
    compose = yaml.safe_load((PRODUCTION / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert "build" not in services["api"]
    assert "build" not in services["frontend"]
    assert services["api"]["image"].startswith("${SCHOLIGHT_BACKEND_IMAGE:")
    assert services["frontend"]["image"].startswith("${SCHOLIGHT_FRONTEND_IMAGE:")
    assert "ports" not in services["api"]
    assert "ports" not in services["frontend"]
    assert services["caddy"]["ports"] == ["80:80", "443:443"]


def test_extract_sidecar_is_internal_hardened_and_health_gated() -> None:
    compose = yaml.safe_load((PRODUCTION / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    extract = services["extract"]

    assert extract["image"].startswith("${SCHOLIGHT_EXTRACT_IMAGE:")
    assert "ports" not in extract
    assert extract["read_only"] is True
    assert extract["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in extract["security_opt"]
    assert services["api"]["depends_on"]["extract"]["condition"] == "service_healthy"
    assert services["api"]["environment"]["SCHOLIGHT_EXTRACT_SERVICE_URL"] == (
        "http://extract:8001"
    )
    assert services["api"]["environment"]["SCHOLIGHT_EXTRACT_ENABLED"] == "true"


def test_extract_runtime_contract_has_stable_token_and_dedicated_ip() -> None:
    runtime = (PRODUCTION / "runtime.env.example").read_text(encoding="utf-8")

    assert "SCHOLIGHT_EXTRACT_IP=172.31.0.25" in runtime
    assert "SCHOLIGHT_EXTRACT_INTERNAL_TOKEN=" in runtime


def test_production_compose_separates_application_and_migration_database_roles() -> None:
    compose = yaml.safe_load((PRODUCTION / "compose.yaml").read_text(encoding="utf-8"))
    api_environment = compose["services"]["api"]["environment"]
    migrate_environment = compose["services"]["migrate"]["environment"]

    assert api_environment["SCHOLIGHT_PG_USER"].startswith("${SCHOLIGHT_APP_PG_USER:")
    assert migrate_environment["SCHOLIGHT_PG_USER"].startswith("${SCHOLIGHT_MIGRATION_PG_USER:")
    assert api_environment["SCHOLIGHT_PG_PASSWORD"].startswith("${SCHOLIGHT_APP_PG_PASSWORD:")
    assert migrate_environment["SCHOLIGHT_PG_PASSWORD"].startswith(
        "${SCHOLIGHT_MIGRATION_PG_PASSWORD:"
    )
    assert api_environment["SCHOLIGHT_PG_USER"] != migrate_environment["SCHOLIGHT_PG_USER"]
    assert api_environment["SCHOLIGHT_PG_PASSWORD"] != migrate_environment["SCHOLIGHT_PG_PASSWORD"]


def test_production_api_forwards_search_defaults() -> None:
    compose = yaml.safe_load((PRODUCTION / "compose.yaml").read_text(encoding="utf-8"))
    environment = compose["services"]["api"]["environment"]

    assert (
        environment.get("SCHOLIGHT_SEARCH_PAPER_CANDIDATE_TOP_K"),
        environment.get("SCHOLIGHT_SEARCH_ENRICHMENT_RPC_TIMEOUT_SECONDS"),
        environment.get("SCHOLIGHT_SEARCH_LEVEL2_RPC_TIMEOUT_SECONDS"),
        environment.get("SCHOLIGHT_SEARCH_LEVEL2_TIMEOUT_SECONDS"),
    ) == (
        "${SCHOLIGHT_SEARCH_PAPER_CANDIDATE_TOP_K:-200}",
        "${SCHOLIGHT_SEARCH_ENRICHMENT_RPC_TIMEOUT_SECONDS:-1.5}",
        "${SCHOLIGHT_SEARCH_LEVEL2_RPC_TIMEOUT_SECONDS:-45.0}",
        "${SCHOLIGHT_SEARCH_LEVEL2_TIMEOUT_SECONDS:-60.0}",
    )


def test_production_runtime_example_documents_search_defaults() -> None:
    runtime = (PRODUCTION / "runtime.env.example").read_text(encoding="utf-8")

    assert all(
        setting in runtime
        for setting in (
            "SCHOLIGHT_SEARCH_PAPER_CANDIDATE_TOP_K=200",
            "SCHOLIGHT_SEARCH_ENRICHMENT_RPC_TIMEOUT_SECONDS=1.5",
            "SCHOLIGHT_SEARCH_LEVEL2_RPC_TIMEOUT_SECONDS=45.0",
            "SCHOLIGHT_SEARCH_LEVEL2_TIMEOUT_SECONDS=60.0",
        )
    )


def test_workflows_do_not_persist_checkout_credentials_or_inherit_secrets() -> None:
    workflows = "\n".join(
        (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        for name in ("ci.yml", "release.yml")
    )

    checkout_count = workflows.count("uses: actions/checkout@")
    assert workflows.count("persist-credentials: false") == checkout_count
    assert "secrets: inherit" not in workflows


def test_caddy_image_is_reviewed_and_not_runtime_overridable() -> None:
    compose_text = (PRODUCTION / "compose.yaml").read_text(encoding="utf-8")
    compose = yaml.safe_load(compose_text)
    caddy_image = compose["services"]["caddy"]["image"]

    assert DIGEST_REFERENCE.fullmatch(caddy_image)
    assert "SCHOLIGHT_CADDY_IMAGE" not in compose_text


def test_migrate_service_receives_database_secrets_only() -> None:
    compose = yaml.safe_load((PRODUCTION / "compose.yaml").read_text(encoding="utf-8"))
    environment = compose["services"]["migrate"]["environment"]
    serialized = "\n".join(environment if isinstance(environment, list) else environment.keys())

    assert "SCHOLIGHT_PG_PASSWORD" in serialized
    assert "SCHOLIGHT_AUTH_JWT_SECRET" not in serialized
    assert "SCHOLIGHT_ANONYMOUS_QUOTA_HMAC_SECRET" not in serialized
    assert "SCHOLIGHT_ACCESS_KEY_HMAC_SECRET" not in serialized
    assert "SCHOLIGHT_ZILLIZ_TOKEN" not in serialized
    assert "SCHOLIGHT_EMBEDDING_API_KEY" not in serialized


def test_release_manifest_examples_are_digest_qualified() -> None:
    values: dict[str, str] = {}
    for line in (PRODUCTION / "release.env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value

    assert values["SCHOLIGHT_RELEASE_CONTRACT_VERSION"] == "2"
    assert len(values["SCHOLIGHT_RELEASE_SHA"]) == 40
    assert DIGEST_REFERENCE.fullmatch(values["SCHOLIGHT_BACKEND_IMAGE"])
    assert DIGEST_REFERENCE.fullmatch(values["SCHOLIGHT_FRONTEND_IMAGE"])
    assert DIGEST_REFERENCE.fullmatch(values["SCHOLIGHT_EXTRACT_IMAGE"])


def test_caddy_blocks_internal_health_and_routes_api_directly() -> None:
    caddyfile = (PRODUCTION / "Caddyfile").read_text(encoding="utf-8")

    assert "https://{$SCHOLIGHT_DOMAIN}" in caddyfile
    assert "@edge_host host {$SCHOLIGHT_EDGE_DOMAIN}" in caddyfile
    assert "@origin_health path /healthz" in caddyfile
    assert "respond @origin_health 200" in caddyfile
    assert "respond 404" in caddyfile
    assert "\troute {" in caddyfile
    assert "/api/livez" in caddyfile
    assert "/api/readyz" in caddyfile
    assert "respond @internal_health 404" in caddyfile
    assert "reverse_proxy api:8000" in caddyfile
    assert "reverse_proxy frontend:8080" in caddyfile
    assert "format json" in caddyfile
    assert "dial_timeout 3s" in caddyfile
    assert "response_header_timeout 65s" in caddyfile
    assert "keepalive 30s" in caddyfile
    assert "lb_try_duration" not in caddyfile
    assert caddyfile.index("respond @internal_health 404") < caddyfile.index("handle_path /api/*")


def test_caddy_receives_only_explicit_public_domains() -> None:
    compose = yaml.safe_load((PRODUCTION / "compose.yaml").read_text(encoding="utf-8"))
    environment = compose["services"]["caddy"]["environment"]

    assert environment["SCHOLIGHT_DOMAIN"] == ("${SCHOLIGHT_DOMAIN:?SCHOLIGHT_DOMAIN is required}")
    assert environment["SCHOLIGHT_EDGE_DOMAIN"] == (
        "${SCHOLIGHT_EDGE_DOMAIN:?SCHOLIGHT_EDGE_DOMAIN is required}"
    )


def test_production_services_have_hard_resource_boundaries() -> None:
    compose = yaml.safe_load((PRODUCTION / "compose.yaml").read_text(encoding="utf-8"))
    expected = {
        "api": ("1024m", "1.5", 256),
        "paper-ingest": ("1280m", "0.5", 128),
        "metadata-sync": ("384m", "0.2", 128),
        "caddy": ("192m", "0.25", 128),
        "frontend": ("128m", "0.1", 64),
        "survey-draft-worker": ("768m", "0.5", 256),
        "survey-worker": ("2560m", "1.0", 512),
    }

    for service_name, (memory, cpus, pids) in expected.items():
        service = compose["services"][service_name]
        assert service["mem_limit"] == memory
        assert service["memswap_limit"] == memory
        assert str(service["cpus"]) == cpus
        assert service["pids_limit"] == pids


def test_production_services_use_bounded_nonblocking_aws_logs() -> None:
    compose = yaml.safe_load((PRODUCTION / "compose.yaml").read_text(encoding="utf-8"))

    for service in compose["services"].values():
        logging = service["logging"]
        assert logging["driver"] == "awslogs"
        assert logging["options"]["awslogs-group"] == "/scholight/production/services"
        assert logging["options"]["mode"] == "non-blocking"
        assert logging["options"]["max-buffer-size"] == "4m"


def test_production_database_pools_are_scoped_per_service() -> None:
    compose = yaml.safe_load((PRODUCTION / "compose.yaml").read_text(encoding="utf-8"))

    assert compose["services"]["api"]["environment"]["SCHOLIGHT_PG_POOL_MIN_SIZE"] == 2
    assert compose["services"]["api"]["environment"]["SCHOLIGHT_PG_POOL_MAX_SIZE"] == 12
    for name in ("metadata-sync", "paper-ingest", "survey-draft-worker", "survey-worker"):
        assert compose["services"][name]["environment"]["SCHOLIGHT_PG_POOL_MIN_SIZE"] == 1
        assert compose["services"][name]["environment"]["SCHOLIGHT_PG_POOL_MAX_SIZE"] == 4


def test_survey_workers_are_opt_in_and_hardened_for_compatibility_release() -> None:
    compose = yaml.safe_load((PRODUCTION / "compose.yaml").read_text(encoding="utf-8"))

    for name in ("survey-draft-worker", "survey-worker"):
        service = compose["services"][name]
        assert service["profiles"] == ["survey"]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["restart"] == "unless-stopped"
        assert service["mem_limit"] == service["memswap_limit"]
    assert compose["services"]["survey-worker"]["volumes"] == ["scholight-data:/data"]
    assert "volumes" not in compose["services"]["survey-draft-worker"]


def test_survey_workers_share_an_explicit_api_service_discovery_contract() -> None:
    compose = yaml.safe_load((PRODUCTION / "compose.yaml").read_text(encoding="utf-8"))
    smoke = (PRODUCTION / "smoke.sh").read_text(encoding="utf-8")

    assert compose["services"]["api"]["networks"]["scholight"]["aliases"] == ["api"]
    for worker in ("survey-draft-worker", "survey-worker"):
        command = (
            f"compose exec -T {worker} \\\n"
            "    curl --fail --silent --show-error http://api:8000/livez"
        )
        assert command in smoke


def test_uvicorn_has_explicit_tunable_connection_boundaries() -> None:
    entrypoint = (ROOT / "docker" / "scholight-api" / "start.py").read_text(encoding="utf-8")

    assert "timeout_keep_alive=settings.server_keep_alive_seconds" in entrypoint
    assert "limit_concurrency=settings.server_limit_concurrency" in entrypoint
    assert "backlog=settings.server_backlog" in entrypoint


def test_source_contains_no_production_dependency_defaults_or_secret_fragments() -> None:
    config = (ROOT / "scholight" / "config.py").read_text(encoding="utf-8")
    store_client = (ROOT / "scholight" / "store" / "client.py").read_text(encoding="utf-8")
    search_engine = (ROOT / "scholight" / "search" / "engine.py").read_text(encoding="utf-8")

    assert "serverless.ali-cn-hangzhou.cloud.zilliz.com.cn" not in config
    assert "openapi-qb-nat.sii.edu.cn" not in config
    assert "cf0m0gegaz1c.ap-east-1.rds.amazonaws.com" not in config
    assert "token=masked" not in store_client
    assert "query=request.query[:80]" not in search_engine
    assert "query_length=len(request.query)" in search_engine


def test_frontend_agent_document_templates_use_canonical_url_placeholder() -> None:
    llms = (FRONTEND_RUNTIME / "llms.txt.template").read_text(encoding="utf-8")
    docs = (FRONTEND_RUNTIME / "docs.md.template").read_text(encoding="utf-8")
    public = ROOT / "frontend" / "public"
    robots = (public / "robots.txt").read_text(encoding="utf-8")

    assert llms.startswith("# Scholight\n")
    assert "> " in llms
    assert "(@@SCHOLIGHT_PUBLIC_WEB_URL@@/docs.md)" in llms
    assert "(@@SCHOLIGHT_PUBLIC_WEB_URL@@/api/openapi.json)" in llms
    assert "(@@SCHOLIGHT_PUBLIC_WEB_URL@@/api/mcp)" in llms
    assert "export SCHOLIGHT_BASE_URL=@@SCHOLIGHT_PUBLIC_WEB_URL@@" in docs
    assert "@@SCHOLIGHT_PUBLIC_WEB_URL@@/api/search" in docs
    assert "@@SCHOLIGHT_PUBLIC_WEB_URL@@/api/mcp" in docs
    assert "@@SCHOLIGHT_PUBLIC_WEB_URL@@/api/openapi.json" in docs
    assert "sk_live_" in docs
    assert "example.org" not in docs
    assert "YOUR_SCHOLIGHT_ORIGIN" not in docs
    assert "User-agent: *" in robots
    assert "Allow: /" in robots


def test_frontend_renders_agent_documents_from_public_web_url(tmp_path: Path) -> None:
    output = tmp_path / "rendered"
    env = {
        **os.environ,
        "SCHOLIGHT_PUBLIC_WEB_URL": "https://scholight.sanchezcloud.net",
    }

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
    assert "https://scholight.sanchezcloud.net/api/mcp" in docs
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
    nginx = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")

    for path in ("/llms.txt", "/.well-known/llms.txt", "/docs.md", "/robots.txt"):
        assert f"location = {path}" in nginx
    assert "default_type text/markdown;" in nginx
    assert "alias /tmp/scholight-docs/llms.txt;" in nginx
    assert "alias /tmp/scholight-docs/docs.md;" in nginx
    assert "try_files /robots.txt =404;" in nginx
    assert "try_files /index.html =404;" in nginx
    assert (
        """add_header Link '</docs.md>; rel="alternate"; type="text/markdown"' always;""" in nginx
    )
    assert 'rel="alternate"' in index
    assert 'type="text/markdown"' in index
    assert 'href="/docs.md"' in index


def test_frontend_runtime_receives_only_the_existing_canonical_url_setting() -> None:
    compose = yaml.safe_load((PRODUCTION / "compose.yaml").read_text(encoding="utf-8"))
    environment = compose["services"]["frontend"]["environment"]
    dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")

    assert environment == {
        "SCHOLIGHT_PUBLIC_WEB_URL": "${SCHOLIGHT_PUBLIC_WEB_URL:?SCHOLIGHT_PUBLIC_WEB_URL is required}"
    }
    assert "runtime/render-docs.sh" in dockerfile
    assert "/docker-entrypoint.d/40-render-scholight-docs.sh" in dockerfile
    assert "runtime/docs.md.template" in dockerfile
    assert "runtime/llms.txt.template" in dockerfile


def test_release_workflow_is_manual_oidc_and_digest_driven() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "id-token: write" in workflow
    assert "configure-aws-credentials@" in workflow
    assert "environment: production" in workflow
    assert "steps.build.outputs.digest" in workflow
    assert ":latest" not in workflow
    assert "AWS_ACCESS_KEY_ID" not in workflow
    assert "AWS_SECRET_ACCESS_KEY" not in workflow


def test_release_workflow_verifies_package_and_waits_for_terminal_ssm_status() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "package_sha" in workflow
    assert "PackageSha" in workflow
    assert "deploy/production/wait-ssm.sh" in workflow
    assert "aws ssm wait command-executed" not in workflow


def test_release_workflow_uses_fixed_bootstrap_document() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "--document-name Scholight-BootstrapAndRelease" in workflow
    assert "AWS-RunShellScript" not in workflow
    assert "Operation:" in workflow
    assert "AwsRegion:" in workflow
    assert "ReleaseSha:" in workflow
    assert "PackageSha:" in workflow
    assert "BackendImage:" in workflow
    assert "FrontendImage:" in workflow
    assert "ExtractImage:" in workflow


def test_ssm_document_has_only_fixed_validated_release_parameters() -> None:
    document = yaml.safe_load((PRODUCTION / "ssm-document.yaml").read_text(encoding="utf-8"))
    parameters = document["parameters"]

    assert document["schemaVersion"] == "2.2"
    assert document["description"].startswith("Bootstrap and release Scholight")
    assert set(parameters) == {
        "Operation",
        "AwsRegion",
        "ReleaseSha",
        "PackageSha",
        "BackendImage",
        "FrontendImage",
        "ExtractImage",
    }
    assert parameters["Operation"]["allowedValues"] == ["deploy", "rollback"]
    assert parameters["AwsRegion"]["allowedValues"] == ["ap-southeast-1"]
    assert parameters["ReleaseSha"]["allowedPattern"] == r"^$|^[0-9a-f]{40}$"
    assert parameters["PackageSha"]["allowedPattern"] == r"^$|^[0-9a-f]{64}$"
    assert (
        r"683390797772\.dkr\.ecr\.ap-southeast-1\.amazonaws\.com/scholight/backend"
        in (parameters["BackendImage"]["allowedPattern"])
    )
    assert (
        r"683390797772\.dkr\.ecr\.ap-southeast-1\.amazonaws\.com/scholight/frontend"
        in (parameters["FrontendImage"]["allowedPattern"])
    )
    assert (
        r"683390797772\.dkr\.ecr\.ap-southeast-1\.amazonaws\.com/scholight/extract"
        in (parameters["ExtractImage"]["allowedPattern"])
    )
    assert all(value.get("interpolationType") == "ENV_VAR" for value in parameters.values())

    serialized = (PRODUCTION / "ssm-document.yaml").read_text(encoding="utf-8")
    assert "AWS-RunShellScript" not in serialized
    assert "/scholight/production/runtime-env" not in serialized
    assert "docker cp" in serialized
    assert "/opt/scholight-package" in serialized


def test_ssm_document_embedded_shell_is_syntactically_valid() -> None:
    document = yaml.safe_load((PRODUCTION / "ssm-document.yaml").read_text(encoding="utf-8"))
    script = document["mainSteps"][0]["inputs"]["runCommand"][0]

    result = subprocess.run(
        ["bash", "-n"],
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_backend_image_carries_the_complete_host_deployment_package() -> None:
    dockerfile = (ROOT / "docker" / "scholight-api" / "Dockerfile").read_text(encoding="utf-8")

    for name in (
        "bootstrap.sh",
        "compose-command.sh",
        "release.sh",
        "smoke.sh",
        "wait-ssm.sh",
        "compose.yaml",
        "Caddyfile",
        "cloudwatch-agent.json",
        "bootstrap-db.sql",
    ):
        assert f"deploy/production/{name}" in dockerfile
    assert "/opt/scholight-package" in dockerfile
    assert dockerfile.index("/opt/scholight-package") < dockerfile.index("USER scholight")


def test_backend_image_pins_verified_rcm_release() -> None:
    dockerfile = (ROOT / "docker" / "scholight-api" / "Dockerfile").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "ARG RCM_VERSION=v0.2.8" in dockerfile
    assert (
        "ARG RCM_LINUX_X86_64_SHA256="
        "ef73a5d0866ec346a0df37839c697731d29f5a0f5c1a398bd0c0a636a5236684"
    ) in dockerfile
    assert (
        "https://github.com/EricSanchezok/rcm-dist/releases/download/"
        "${RCM_VERSION}/accelerate-x86_64-linux.tar.gz"
    ) in dockerfile
    assert "ARG TARGETARCH" in dockerfile
    assert 'test "${TARGETARCH}" = amd64' in dockerfile
    assert "sha256sum --check" in dockerfile
    assert "COPY --from=builder /app/bin/accelerate /usr/local/bin/accelerate" in dockerfile
    assert "/releases/latest/" not in dockerfile
    assert "test -x /usr/local/bin/accelerate" in workflow
    assert "/usr/local/bin/accelerate --help" in workflow
    assert "poppler-utils" in dockerfile
    assert "pdftotext /tmp/fallback.pdf" in workflow


def test_bootstrap_is_part_of_the_release_package_digest() -> None:
    release_script = (PRODUCTION / "release.sh").read_text(encoding="utf-8")

    assert '"${SCRIPT_DIR}/bootstrap.sh"' in release_script
    assert '"${SCRIPT_DIR}/compose-command.sh"' in release_script
    assert '"${SCRIPT_DIR}/cloudwatch-agent.json"' in release_script


def test_release_and_smoke_share_one_survey_profile_decision() -> None:
    release = (PRODUCTION / "release.sh").read_text(encoding="utf-8")
    smoke = (PRODUCTION / "smoke.sh").read_text(encoding="utf-8")
    helper = (PRODUCTION / "compose-command.sh").read_text(encoding="utf-8")

    assert (
        'COMPOSE_COMMAND=${SCHOLIGHT_COMPOSE_COMMAND:-"${SCRIPT_DIR}/compose-command.sh"}'
        in release
    )
    assert (
        'COMPOSE_COMMAND=${SCHOLIGHT_COMPOSE_COMMAND:-"${SCRIPT_DIR}/compose-command.sh"}' in smoke
    )
    assert "--profile survey" in helper
    assert "SCHOLIGHT_SURVEY_ENABLED must be exactly true or false" in helper
    assert "--profile survey" not in release
    assert "--profile survey" not in smoke


def test_compose_helper_applies_survey_profile_only_when_enabled(tmp_path: Path) -> None:
    helper = PRODUCTION / "compose-command.sh"
    runtime = tmp_path / "runtime.env"
    release = tmp_path / "release.env"
    compose = tmp_path / "compose.yaml"
    fake_bin = tmp_path / "bin"
    log = tmp_path / "docker.log"
    fake_bin.mkdir()
    release.write_text("SCHOLIGHT_BACKEND_IMAGE=fixture\n", encoding="utf-8")
    compose.write_text("services: {}\n", encoding="utf-8")
    docker = fake_bin / "docker"
    docker.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >"${FAKE_DOCKER_LOG}"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        "SCHOLIGHT_RUNTIME_ENV": str(runtime),
        "SCHOLIGHT_RELEASE_ENV": str(release),
        "SCHOLIGHT_COMPOSE_FILE": str(compose),
    }

    runtime.write_text("SCHOLIGHT_SURVEY_ENABLED=false\n", encoding="utf-8")
    disabled = subprocess.run(
        ["bash", str(helper), "ps"], env=environment, capture_output=True, text=True, check=False
    )
    disabled_command = log.read_text(encoding="utf-8")
    runtime.write_text("SCHOLIGHT_SURVEY_ENABLED=true\n", encoding="utf-8")
    enabled = subprocess.run(
        ["bash", str(helper), "ps"], env=environment, capture_output=True, text=True, check=False
    )
    enabled_command = log.read_text(encoding="utf-8")

    assert disabled.returncode == 0
    assert "--profile survey" not in disabled_command
    assert enabled.returncode == 0
    assert "--profile survey ps" in enabled_command


def test_observability_template_has_bounded_retention_and_required_alarms() -> None:
    template = _load_observability_template()
    resources = template["Resources"]
    assert isinstance(resources, dict)

    assert resources["ServiceLogGroup"]["Properties"]["RetentionInDays"] == 14
    assert resources["HostLogGroup"]["Properties"]["RetentionInDays"] == 14
    assert resources["Dashboard"]["Properties"]["DashboardName"] == "Scholight-Production"
    expected = {
        "StatusCheckAlarm",
        "MemoryAlarm",
        "DiskAlarm",
        "SwapAlarm",
        "CpuAlarm",
        "OomAlarm",
        "Unexpected5xxAlarm",
        "StandardLatencyAlarm",
        "ThoroughLatencyAlarm",
        "DeadIngestionAlarm",
        "SurveyContractAlarm",
        "SurveyRuntimeFailureAlarm",
        "SurveyDiagnosticsFailureAlarm",
        "SurveyStalledAlarm",
        "SurveyEmailDeadAlarm",
        "SurveyEmailBacklogAlarm",
        "Proxy502Metric",
        "Proxy504Metric",
        "ProxyConnectionResetMetric",
        "ProxyConnectErrorMetric",
    }
    assert expected.issubset(resources)
    assert "CapacityAlarm" not in resources

    dashboard = resources["Dashboard"]["Properties"]["DashboardBody"]["Sub"]
    parsed_dashboard = json.loads(dashboard)
    assert isinstance(parsed_dashboard["widgets"], list)
    assert "Search in-flight and throughput" in dashboard
    assert "Search stage latency p95" in dashboard
    assert "HTTPX and thread-pool wait p95" in dashboard
    assert "Proxy and application errors" in dashboard
    assert "Background analytics queues" in dashboard
    assert "Survey outcomes and duration" in dashboard
    assert "Survey failures and activity" in dashboard
    assert "Survey email delivery" in dashboard
    assert (
        resources["HostOomMetric"]["Properties"]["FilterPattern"]
        == '?"Out of memory" ?"Killed process" ?"oom-kill"'
    )


def test_survey_smoke_checks_diagnostics_and_contract_audit() -> None:
    source = (ROOT / "scholight" / "cli" / "survey.py").read_text(encoding="utf-8")

    assert "_verify_diagnostic_workspace" in source
    assert '"diagnostics_writable": True' in source
    assert '"workflow_contract": workflow_audit_payload()' in source


def test_survey_email_delivery_has_profile_safe_config_and_smoke_visibility() -> None:
    compose = yaml.safe_load((PRODUCTION / "compose.yaml").read_text(encoding="utf-8"))
    survey_environment = compose["x-survey-environment"]
    smoke = (PRODUCTION / "smoke.sh").read_text(encoding="utf-8")
    runtime = (PRODUCTION / "runtime.env.example").read_text(encoding="utf-8")

    for name in (
        "SCHOLIGHT_ALIYUN_DM_ACCESS_KEY_ID",
        "SCHOLIGHT_ALIYUN_DM_ACCESS_KEY_SECRET",
        "SCHOLIGHT_ALIYUN_DM_ACCOUNT_NAME",
    ):
        assert survey_environment[name] == f"${{{name}:-}}"
        assert f"{name}=" in runtime
    assert "scholight survey status --json-output" in smoke
    assert "requested completion" in runtime


def test_production_has_no_unreviewed_capacity_enforcement_settings() -> None:
    compose = yaml.safe_load((PRODUCTION / "compose.yaml").read_text(encoding="utf-8"))
    environment = compose["services"]["api"]["environment"]

    assert not any("CAPACITY" in name or "MAX_IN_FLIGHT" in name for name in environment)


def test_observability_instance_policy_cannot_read_secrets_or_change_data() -> None:
    template = _load_observability_template()
    statements = template["Resources"]["InstanceObservabilityPolicy"]["Properties"][
        "PolicyDocument"
    ]["Statement"]
    actions = {
        action
        for statement in statements
        for action in (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
    }

    assert actions == {
        "cloudwatch:PutMetricData",
        "logs:CreateLogStream",
        "logs:DescribeLogStreams",
        "logs:PutLogEvents",
    }
    serialized = (PRODUCTION / "observability.yaml").read_text(encoding="utf-8")
    assert "ssm:GetParameter" not in serialized
    assert "zilliz" not in serialized.lower()
    assert "rds:" not in serialized.lower()


def test_ci_validates_bootstrap_with_shellcheck() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "bash -n deploy/production/bootstrap.sh" in workflow
    assert "shellcheck deploy/production/bootstrap.sh" in workflow
    assert "sh -n frontend/runtime/render-docs.sh" in workflow
    assert "shellcheck -s sh frontend/runtime/render-docs.sh" in workflow
    assert "Verify backend host package" in workflow
    assert "/opt/scholight-package/${name}" in workflow
    assert "tests/deploy/test_keepalive_integration.sh" in workflow
    assert "tests/deploy/test_edge_ingress_integration.sh" in workflow


def test_host_smoke_uses_local_tls_ingress_instead_of_public_ip_hairpin() -> None:
    smoke = (PRODUCTION / "smoke.sh").read_text(encoding="utf-8")

    assert "--resolve" in smoke
    assert "${SCHOLIGHT_DOMAIN}:443:127.0.0.1" in smoke
    assert "${SCHOLIGHT_EDGE_DOMAIN}:80:127.0.0.1" in smoke
    assert "http://127.0.0.1/healthz" in smoke
    assert "http://${SCHOLIGHT_EDGE_DOMAIN}/api/openapi.json" in smoke
    assert "https://${SCHOLIGHT_DOMAIN}/healthz" in smoke
    assert "https://${SCHOLIGHT_DOMAIN}/api/openapi.json" in smoke
    assert "https://${SCHOLIGHT_DOMAIN}/llms.txt" in smoke
    assert "https://${SCHOLIGHT_DOMAIN}/docs.md" in smoke


def test_release_workflow_checks_ingress_from_an_external_runner() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "PRODUCTION_DOMAIN: ${{ vars.PRODUCTION_DOMAIN }}" in workflow
    assert "Verify public deployment from the runner" in workflow
    assert "https://${PRODUCTION_DOMAIN}/healthz" in workflow
    assert "https://${PRODUCTION_DOMAIN}/api/openapi.json" in workflow
    assert "https://${PRODUCTION_DOMAIN}/llms.txt" in workflow
    assert "https://${PRODUCTION_DOMAIN}/docs.md" in workflow


def test_external_actions_are_pinned_to_full_commit_shas() -> None:
    action_reference = re.compile(r"^\s*uses:\s*([^\s]+)@([^\s#]+)", re.MULTILINE)
    for name in ("ci.yml", "release.yml"):
        workflow = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        for action, revision in action_reference.findall(workflow):
            if action.startswith("./"):
                continue
            assert re.fullmatch(r"[0-9a-f]{40}", revision), f"{action}@{revision} is mutable"


def test_production_proxy_trust_is_exact_caddy_ip() -> None:
    compose = yaml.safe_load((PRODUCTION / "compose.yaml").read_text(encoding="utf-8"))
    trusted = compose["services"]["api"]["environment"]["SCHOLIGHT_FORWARDED_ALLOW_IPS"]

    assert trusted == "${SCHOLIGHT_CADDY_IP:?SCHOLIGHT_CADDY_IP is required}"
    assert trusted != "*"


def test_long_lived_production_services_have_unique_static_ips() -> None:
    compose = yaml.safe_load((PRODUCTION / "compose.yaml").read_text(encoding="utf-8"))
    expected = {
        "caddy": "${SCHOLIGHT_CADDY_IP:?SCHOLIGHT_CADDY_IP is required}",
        "frontend": "${SCHOLIGHT_FRONTEND_IP:?SCHOLIGHT_FRONTEND_IP is required}",
        "api": "${SCHOLIGHT_API_IP:?SCHOLIGHT_API_IP is required}",
        "metadata-sync": ("${SCHOLIGHT_METADATA_SYNC_IP:?SCHOLIGHT_METADATA_SYNC_IP is required}"),
        "paper-ingest": ("${SCHOLIGHT_PAPER_INGEST_IP:?SCHOLIGHT_PAPER_INGEST_IP is required}"),
    }

    configured = {
        name: compose["services"][name]["networks"]["scholight"]["ipv4_address"]
        for name in expected
    }
    assert configured == expected
    assert len(set(configured.values())) == len(configured)

    runtime = (PRODUCTION / "runtime.env.example").read_text(encoding="utf-8")
    for setting in (
        "SCHOLIGHT_DOCKER_SUBNET=172.31.0.0/24",
        "SCHOLIGHT_CADDY_IP=172.31.0.2",
        "SCHOLIGHT_FRONTEND_IP=172.31.0.10",
        "SCHOLIGHT_API_IP=172.31.0.20",
        "SCHOLIGHT_METADATA_SYNC_IP=172.31.0.30",
        "SCHOLIGHT_PAPER_INGEST_IP=172.31.0.40",
    ):
        assert setting in runtime


def test_ci_compose_validation_sets_the_complete_network_contract() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    for setting in (
        "SCHOLIGHT_DOCKER_SUBNET: 172.31.0.0/24",
        "SCHOLIGHT_CADDY_IP: 172.31.0.2",
        "SCHOLIGHT_FRONTEND_IP: 172.31.0.10",
        "SCHOLIGHT_API_IP: 172.31.0.20",
        "SCHOLIGHT_METADATA_SYNC_IP: 172.31.0.30",
        "SCHOLIGHT_PAPER_INGEST_IP: 172.31.0.40",
    ):
        assert setting in workflow


def test_ci_runs_built_backend_migrations_against_postgres() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "services:" in workflow
    assert "postgres:" in workflow
    assert "load: true" in workflow
    assert "tags: scholight-api:ci" in workflow
    assert "for _ in 1 2; do" in workflow
    assert "/app/.venv/bin/scholight store migrate" in workflow
    assert "to_regclass('auth.schema_migrations')" in workflow
    assert "to_regclass('scholight.schema_migrations')" in workflow
    assert "to_regclass('scholight.search_history')" in workflow
    assert "to_regclass('scholight.anonymous_daily_search_usage')" in workflow
    assert "WHERE schemaname = 'public'" in workflow


def test_sanchezcloud_identity_checkout_path_is_not_a_tracked_symlink() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "--stage", "sanchezcloud-identity"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert not tracked.startswith("120000 ")


def test_database_bootstrap_is_reviewed_and_ci_exercises_least_privilege_roles() -> None:
    bootstrap = (PRODUCTION / "bootstrap-db.sql").read_text(encoding="utf-8")
    release_script = (PRODUCTION / "release.sh").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "bootstrap-db.sql" in release_script
    assert "ALTER DEFAULT PRIVILEGES" in bootstrap
    assert "CREATE SCHEMA IF NOT EXISTS auth AUTHORIZATION %I" in bootstrap
    assert "CREATE SCHEMA IF NOT EXISTS scholight AUTHORIZATION %I" in bootstrap
    assert "GRANT REFERENCES ON TABLE auth.users" in bootstrap
    assert "GRANT SELECT, INSERT, UPDATE ON TABLE auth.users" in bootstrap
    assert "GRANT SELECT, INSERT, UPDATE ON TABLE auth.user_clients" in bootstrap
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE auth.users" not in bootstrap
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE auth.user_clients" not in bootstrap
    assert 'FOR ROLE :"auth_migrator_role"' not in bootstrap
    assert "SCHOLIGHT_PG_USER=scholight_migrator" in workflow
    assert "AUTH_DATABASE_URL=" in workflow
    assert "-U scholight_app" in workflow
    assert "identity_runtime_smoke.py" in workflow
    assert "REVOKE ALL ON TABLE auth.schema_migrations" in bootstrap
    assert "REVOKE ALL ON TABLE scholight.schema_migrations" in bootstrap
    assert "CREATE TABLE public.app_role_must_not_create" in workflow


def test_transition_reconciliation_runbook_covers_all_interruption_states() -> None:
    readme = (PRODUCTION / "README.md").read_text(encoding="utf-8")

    assert "deploy / activating" in readme
    assert "deploy / activated" in readme
    assert "rollback / activating" in readme
    assert "rollback / activated" in readme
    assert "Never delete `transition.env` before" in readme
