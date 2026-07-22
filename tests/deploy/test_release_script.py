"""Release script safety contract tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "deploy" / "production" / "release.sh"
DIGEST = "sha256:" + "a" * 64


def run_release(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=ROOT,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def deployment_environment(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.log"
    runtime = tmp_path / "runtime.env"
    runtime.write_text(
        "SCHOLIGHT_AWS_REGION=ap-east-1\nSCHOLIGHT_ECR_REGISTRY=registry.example\n",
        encoding="utf-8",
    )
    runtime.chmod(0o600)
    make_executable(
        bin_dir / "aws",
        "#!/usr/bin/env bash\n"
        'printf \'aws %s\\n\' "$*" >>"${FAKE_COMMAND_LOG}"\n'
        "printf 'fixture-password\\n'\n",
    )
    make_executable(
        bin_dir / "docker",
        "#!/usr/bin/env bash\n"
        'printf \'docker %s\\n\' "$*" >>"${FAKE_COMMAND_LOG}"\n'
        "if [[ \"$*\" == 'login --username AWS --password-stdin '* ]]; then cat >/dev/null; fi\n"
        "if [[ \"$*\" == *'--profile migrate run --rm migrate'* && "
        "${FAKE_MIGRATE_FAIL:-0} == 1 ]]; then exit 42; fi\n",
    )
    make_executable(bin_dir / "flock", "#!/usr/bin/env bash\nexit 0\n")
    make_executable(
        tmp_path / "smoke.sh",
        "#!/usr/bin/env bash\n"
        'printf \'smoke %s\\n\' "${SCHOLIGHT_RELEASE_ENV}" >>"${FAKE_COMMAND_LOG}"\n'
        "if [[ ${FAKE_CANDIDATE_SMOKE_FAIL:-0} == 1 && "
        "${SCHOLIGHT_RELEASE_ENV} == *'/candidate.'* ]]; then exit 43; fi\n",
    )
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SCHOLIGHT_STATE_DIR": str(tmp_path / "state"),
        "SCHOLIGHT_RUNTIME_ENV": str(runtime),
        "SCHOLIGHT_SMOKE_SCRIPT": str(tmp_path / "smoke.sh"),
        "FAKE_COMMAND_LOG": str(log),
    }


def production_package_sha() -> str:
    result = run_release("package-sha")
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def deploy_arguments(release_sha: str = "a" * 40) -> tuple[str, ...]:
    return (
        "deploy",
        "--contract-version",
        "1",
        "--package-sha",
        production_package_sha(),
        "--release-sha",
        release_sha,
        "--backend-image",
        f"registry.example/scholight/backend@{DIGEST}",
        "--frontend-image",
        f"registry.example/scholight/frontend@{DIGEST}",
    )


def test_deploy_rejects_tag_only_image_before_external_commands(tmp_path: Path) -> None:
    result = run_release(
        "deploy",
        "--contract-version",
        "1",
        "--release-sha",
        "a" * 40,
        "--backend-image",
        "registry.example/scholight/backend:latest",
        "--frontend-image",
        f"registry.example/scholight/frontend@{DIGEST}",
        env={"SCHOLIGHT_STATE_DIR": str(tmp_path)},
    )

    assert result.returncode != 0
    assert "digest-qualified" in result.stderr


def test_rollback_refuses_without_previous_release(tmp_path: Path) -> None:
    result = run_release("rollback", env={"SCHOLIGHT_STATE_DIR": str(tmp_path)})

    assert result.returncode != 0
    assert "previous release" in result.stderr


def test_deploy_rejects_mismatched_production_package(tmp_path: Path) -> None:
    args = list(deploy_arguments())
    package_index = args.index("--package-sha") + 1
    args[package_index] = "f" * 64

    result = run_release(*args, env={"SCHOLIGHT_STATE_DIR": str(tmp_path)})

    assert result.returncode != 0
    assert "does not match" in result.stderr


def test_rollback_logs_in_and_pulls_previous_images_before_activation(tmp_path: Path) -> None:
    env = deployment_environment(tmp_path)
    state = Path(env["SCHOLIGHT_STATE_DIR"])
    state.mkdir()
    manifest = (
        "SCHOLIGHT_RELEASE_CONTRACT_VERSION=1\n"
        f"SCHOLIGHT_PACKAGE_SHA={production_package_sha()}\n"
        f"SCHOLIGHT_RELEASE_SHA={'b' * 40}\n"
        f"SCHOLIGHT_BACKEND_IMAGE=registry.example/scholight/backend@{DIGEST}\n"
        f"SCHOLIGHT_FRONTEND_IMAGE=registry.example/scholight/frontend@{DIGEST}\n"
    )
    (state / "current.env").write_text(manifest.replace("b" * 40, "c" * 40), encoding="utf-8")
    (state / "previous.env").write_text(manifest, encoding="utf-8")

    result = run_release("rollback", env=env)

    commands = Path(env["FAKE_COMMAND_LOG"]).read_text(encoding="utf-8")
    login_position = commands.index("docker login --username AWS --password-stdin")
    pull_position = commands.index(" pull api frontend")
    activate_position = commands.index(" up -d ")
    assert result.returncode == 0 and login_position < pull_position < activate_position


def test_release_scripts_do_not_execute_runtime_env_or_prune_images() -> None:
    scripts = "\n".join(
        (ROOT / "deploy" / "production" / name).read_text(encoding="utf-8")
        for name in ("release.sh", "smoke.sh")
    )

    assert 'source "${RUNTIME_ENV}"' not in scripts
    assert "docker system prune" not in scripts
    assert "docker image prune" not in scripts


def test_release_refuses_new_operation_when_transition_needs_reconciliation(tmp_path: Path) -> None:
    env = deployment_environment(tmp_path)
    state = Path(env["SCHOLIGHT_STATE_DIR"])
    state.mkdir()
    (state / "transition.env").write_text(
        "SCHOLIGHT_TRANSITION_OPERATION=deploy\nSCHOLIGHT_TRANSITION_STAGE=activated\n",
        encoding="utf-8",
    )

    result = run_release(*deploy_arguments(), env=env)

    assert result.returncode != 0
    assert "reconciliation" in result.stderr


def test_status_reports_unfinished_transition(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "transition.env").write_text(
        "SCHOLIGHT_TRANSITION_OPERATION=rollback\nSCHOLIGHT_TRANSITION_STAGE=activating\n",
        encoding="utf-8",
    )

    result = run_release("status", env={"SCHOLIGHT_STATE_DIR": str(state)})

    assert result.returncode != 0
    assert "Unfinished transition" in result.stdout


def test_deploy_rejects_runtime_env_with_group_or_other_permissions(tmp_path: Path) -> None:
    env = deployment_environment(tmp_path)
    Path(env["SCHOLIGHT_RUNTIME_ENV"]).chmod(0o644)

    result = run_release(*deploy_arguments(), env=env)

    assert result.returncode != 0
    assert "mode 0600" in result.stderr


def test_deploy_rejects_runtime_env_symlink(tmp_path: Path) -> None:
    env = deployment_environment(tmp_path)
    runtime = Path(env["SCHOLIGHT_RUNTIME_ENV"])
    target = tmp_path / "runtime-target.env"
    runtime.rename(target)
    runtime.symlink_to(target)

    result = run_release(*deploy_arguments(), env=env)

    assert result.returncode != 0
    assert "regular non-symlink" in result.stderr


def test_migration_failure_does_not_activate_candidate(tmp_path: Path) -> None:
    env = deployment_environment(tmp_path)
    env["FAKE_MIGRATE_FAIL"] = "1"

    result = run_release(*deploy_arguments(), env=env)

    commands = Path(env["FAKE_COMMAND_LOG"]).read_text(encoding="utf-8")
    assert result.returncode != 0 and " up -d " not in commands


def test_successful_deploy_promotes_candidate_after_migration(tmp_path: Path) -> None:
    env = deployment_environment(tmp_path)

    result = run_release(*deploy_arguments(), env=env)

    current = Path(env["SCHOLIGHT_STATE_DIR"]) / "current.env"
    assert result.returncode == 0 and "SCHOLIGHT_RELEASE_SHA=" + "a" * 40 in current.read_text()


def test_candidate_smoke_failure_restores_current_without_changing_previous(tmp_path: Path) -> None:
    env = deployment_environment(tmp_path)
    state = Path(env["SCHOLIGHT_STATE_DIR"])
    state.mkdir()
    current = state / "current.env"
    previous = state / "previous.env"
    current.write_text("SCHOLIGHT_RELEASE_SHA=" + "b" * 40 + "\n", encoding="utf-8")
    previous.write_text("SCHOLIGHT_RELEASE_SHA=" + "c" * 40 + "\n", encoding="utf-8")
    env["FAKE_CANDIDATE_SMOKE_FAIL"] = "1"

    result = run_release(*deploy_arguments(), env=env)

    assert (
        result.returncode != 0
        and current.read_text(encoding="utf-8") == "SCHOLIGHT_RELEASE_SHA=" + "b" * 40 + "\n"
        and previous.read_text(encoding="utf-8") == "SCHOLIGHT_RELEASE_SHA=" + "c" * 40 + "\n"
    )
