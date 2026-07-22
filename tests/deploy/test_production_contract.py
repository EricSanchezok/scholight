"""Static contracts for the production deployment package."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
PRODUCTION = ROOT / "deploy" / "production"
DIGEST_REFERENCE = re.compile(r"^[^\s:@]+(?:/[^\s:@]+)+@sha256:[0-9a-f]{64}$")


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


def test_caddy_image_is_reviewed_and_not_runtime_overrideable() -> None:
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

    assert values["SCHOLIGHT_RELEASE_CONTRACT_VERSION"] == "1"
    assert len(values["SCHOLIGHT_RELEASE_SHA"]) == 40
    assert DIGEST_REFERENCE.fullmatch(values["SCHOLIGHT_BACKEND_IMAGE"])
    assert DIGEST_REFERENCE.fullmatch(values["SCHOLIGHT_FRONTEND_IMAGE"])


def test_caddy_blocks_internal_health_and_routes_api_directly() -> None:
    caddyfile = (PRODUCTION / "Caddyfile").read_text(encoding="utf-8")

    assert "/api/livez" in caddyfile
    assert "/api/readyz" in caddyfile
    assert "respond @internal_health 404" in caddyfile
    assert "reverse_proxy api:8000" in caddyfile
    assert "reverse_proxy frontend:8080" in caddyfile
    assert "format json" in caddyfile


def test_caddy_routes_openpaper_over_the_shared_edge_network() -> None:
    compose = yaml.safe_load((PRODUCTION / "compose.yaml").read_text(encoding="utf-8"))
    caddyfile = (PRODUCTION / "Caddyfile").read_text(encoding="utf-8")

    assert "edge" in compose["services"]["caddy"]["networks"]
    assert compose["networks"]["edge"] == {
        "external": True,
        "name": "${SANCHEZCLOUD_EDGE_NETWORK:-sanchezcloud-edge}",
    }
    assert "{$OPENPAPER_DOMAIN}" in caddyfile
    assert "reverse_proxy openpaper-api:8000" in caddyfile
    assert "reverse_proxy openpaper-client:3000" in caddyfile


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
    assert "--package-sha" in workflow
    assert "deploy/production/wait-ssm.sh" in workflow
    assert "aws ssm wait command-executed" not in workflow


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

    assert trusted == "${SCHOLIGHT_CADDY_IP:-172.31.0.2}"
    assert trusted != "*"


def test_ci_runs_built_backend_migrations_against_postgres() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "services:" in workflow
    assert "postgres:" in workflow
    assert "load: true" in workflow
    assert "tags: scholight-api:ci" in workflow
    assert "for _ in 1 2; do" in workflow
    assert "/app/.venv/bin/scholight store migrate" in workflow
    assert "to_regclass('public._migrations')" in workflow
    assert "to_regclass('public.search_history')" in workflow
    assert "to_regclass('public.anonymous_daily_search_usage')" in workflow


def test_cloud_auth_checkout_path_is_not_a_tracked_symlink() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "--stage", "cloud-auth"],
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
    assert "ALTER TABLE %I.%I OWNER TO %I" in bootstrap
    assert "SCHOLIGHT_PG_USER=scholight_migrator" in workflow
    assert "-U scholight_app" in workflow
    assert "REVOKE ALL ON TABLE public.%I" in bootstrap
    assert "CREATE TABLE public.app_role_must_not_create" in workflow


def test_transition_reconciliation_runbook_covers_all_interruption_states() -> None:
    readme = (PRODUCTION / "README.md").read_text(encoding="utf-8")

    assert "deploy / activating" in readme
    assert "deploy / activated" in readme
    assert "rollback / activating" in readme
    assert "rollback / activated" in readme
    assert "Never delete `transition.env` before" in readme
