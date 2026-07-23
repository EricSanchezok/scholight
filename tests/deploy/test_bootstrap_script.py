"""Idempotent production host bootstrap tests."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
BOOTSTRAP = ROOT / "deploy" / "production" / "bootstrap.sh"
PACKAGE_NAMES = (
    "compose.yaml",
    "Caddyfile",
    "bootstrap-db.sql",
    "bootstrap.sh",
    "release.sh",
    "smoke.sh",
    "wait-ssm.sh",
)
RELEASE_SHA = "a" * 40
BACKEND_IMAGE = (
    "683390797772.dkr.ecr.ap-southeast-1.amazonaws.com/scholight/backend@sha256:" + "b" * 64
)
FRONTEND_IMAGE = (
    "683390797772.dkr.ecr.ap-southeast-1.amazonaws.com/scholight/frontend@sha256:" + "c" * 64
)


def make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def runtime_contents() -> str:
    return "\n".join(
        (
            "SCHOLIGHT_DOMAIN=scholight.example.invalid",
            "SCHOLIGHT_ACME_EMAIL=operator@example.invalid",
            "SCHOLIGHT_AWS_REGION=ap-southeast-1",
            "SCHOLIGHT_ECR_REGISTRY=683390797772.dkr.ecr.ap-southeast-1.amazonaws.com",
            "SCHOLIGHT_PG_HOST=postgres.example.invalid",
            "SCHOLIGHT_PG_DATABASE=sanchezcloud",
            "SCHOLIGHT_APP_PG_USER=scholight_app",
            "SCHOLIGHT_APP_PG_PASSWORD=app-password",
            "SCHOLIGHT_MIGRATION_PG_USER=scholight_migrator",
            "SCHOLIGHT_MIGRATION_PG_PASSWORD=migrator-password",
            "SCHOLIGHT_ZILLIZ_URI=https://zilliz.example.invalid",
            "SCHOLIGHT_ZILLIZ_TOKEN=zilliz-token",
            "SCHOLIGHT_EMBEDDING_BASE_URL=https://embedding.example.invalid/v1",
            "SCHOLIGHT_EMBEDDING_API_KEY=embedding-key",
            "SCHOLIGHT_EMBEDDING_MODEL=fixture-model",
            "SCHOLIGHT_AUTH_JWT_SECRET=fixture-secret-at-least-thirty-two-bytes",
            "SCHOLIGHT_ANONYMOUS_QUOTA_HMAC_SECRET=anonymous-secret-at-least-thirty-two-bytes",
            "SCHOLIGHT_ACCESS_KEY_HMAC_SECRET=access-secret-at-least-thirty-two-bytes",
            "SCHOLIGHT_PUBLIC_WEB_URL=https://scholight.example.invalid",
            'SCHOLIGHT_CORS_ALLOW_ORIGINS=["https://scholight.example.invalid"]',
            "",
        )
    )


def package_sha(package: Path) -> str:
    inventory = "".join(
        f"{name}:{hashlib.sha256((package / name).read_bytes()).hexdigest()}\n"
        for name in PACKAGE_NAMES
    )
    return hashlib.sha256(inventory.encode()).hexdigest()


def bootstrap_environment(tmp_path: Path, runtime: str | None) -> tuple[dict[str, str], Path, Path]:
    fake_root = tmp_path / "root"
    source = tmp_path / "package"
    bin_dir = tmp_path / "bin"
    log = tmp_path / "commands.log"
    (fake_root / "etc").mkdir(parents=True)
    (fake_root / "etc" / "os-release").write_text(
        'ID="amzn"\nVERSION_ID="2023"\n', encoding="utf-8"
    )
    source.mkdir()
    bin_dir.mkdir()

    for name in PACKAGE_NAMES:
        if name == "bootstrap.sh":
            (source / name).write_bytes(BOOTSTRAP.read_bytes())
        elif name == "release.sh":
            make_executable(
                source / name,
                '#!/usr/bin/env bash\nprintf \'release %s\\n\' "$*" >>"${FAKE_COMMAND_LOG}"\n',
            )
        else:
            (source / name).write_text(f"fixture {name}\n", encoding="utf-8")
    for name in ("smoke.sh", "wait-ssm.sh"):
        (source / name).chmod(0o755)

    make_executable(
        bin_dir / "docker",
        "#!/usr/bin/env bash\n"
        'printf \'docker %s\\n\' "$*" >>"${FAKE_COMMAND_LOG}"\n'
        "if [[ \"$*\" == 'compose version --short' ]]; then printf 'v2.40.3\\n'; fi\n",
    )
    make_executable(
        bin_dir / "systemctl",
        '#!/usr/bin/env bash\nprintf \'systemctl %s\\n\' "$*" >>"${FAKE_COMMAND_LOG}"\n',
    )
    make_executable(bin_dir / "uname", "#!/usr/bin/env bash\nprintf 'x86_64\\n'\n")
    make_executable(bin_dir / "flock", "#!/usr/bin/env bash\nexit 0\n")
    make_executable(
        bin_dir / "aws",
        "#!/usr/bin/env bash\n"
        'printf \'aws %s\\n\' "$*" >>"${FAKE_COMMAND_LOG}"\n'
        "printf '%s' \"${FAKE_PARAMETER_VALUE}\"\n",
    )

    runtime_path = fake_root / "etc" / "scholight" / "runtime.env"
    if runtime is not None:
        runtime_path.parent.mkdir()
        runtime_path.write_text(runtime, encoding="utf-8")
        runtime_path.chmod(0o600)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SCHOLIGHT_BOOTSTRAP_ROOT": str(fake_root),
        "SCHOLIGHT_SOURCE_PACKAGE_DIR": str(source),
        "FAKE_COMMAND_LOG": str(log),
        "FAKE_PARAMETER_VALUE": runtime_contents(),
    }
    return env, source, log


def run_bootstrap(
    env: dict[str, str], source: Path, expected_sha: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            str(BOOTSTRAP),
            "deploy",
            "--contract-version",
            "1",
            "--package-sha",
            expected_sha or package_sha(source),
            "--release-sha",
            RELEASE_SHA,
            "--backend-image",
            BACKEND_IMAGE,
            "--frontend-image",
            FRONTEND_IMAGE,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_existing_runtime_env_is_never_fetched_or_rewritten(tmp_path: Path) -> None:
    original = runtime_contents()
    env, source, log = bootstrap_environment(tmp_path, original)

    result = run_bootstrap(env, source)

    runtime = Path(env["SCHOLIGHT_BOOTSTRAP_ROOT"]) / "etc" / "scholight" / "runtime.env"
    commands = log.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert runtime.read_text(encoding="utf-8") == original
    assert "aws " not in commands


def test_amazon_linux_os_release_symlink_is_accepted(tmp_path: Path) -> None:
    env, source, _ = bootstrap_environment(tmp_path, runtime_contents())
    fake_root = Path(env["SCHOLIGHT_BOOTSTRAP_ROOT"])
    os_release = fake_root / "etc" / "os-release"
    canonical_os_release = fake_root / "usr" / "lib" / "os-release"
    canonical_os_release.parent.mkdir(parents=True)
    os_release.replace(canonical_os_release)
    os_release.symlink_to("../usr/lib/os-release")

    result = run_bootstrap(env, source)

    assert result.returncode == 0, result.stderr


def test_missing_runtime_env_fetches_only_the_fixed_secure_parameter(tmp_path: Path) -> None:
    env, source, log = bootstrap_environment(tmp_path, None)

    result = run_bootstrap(env, source)

    runtime = Path(env["SCHOLIGHT_BOOTSTRAP_ROOT"]) / "etc" / "scholight" / "runtime.env"
    commands = log.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert runtime.read_text(encoding="utf-8") == runtime_contents().rstrip("\n")
    assert runtime.stat().st_mode & 0o777 == 0o600
    assert (
        "aws ssm get-parameter --name /scholight/production/runtime-env "
        "--with-decryption --query Parameter.Value --output text "
        "--region ap-southeast-1"
    ) in commands


def test_invalid_downloaded_runtime_is_not_installed_or_printed(tmp_path: Path) -> None:
    env, source, _ = bootstrap_environment(tmp_path, None)
    secret = "do-not-print-this-secret"
    env["FAKE_PARAMETER_VALUE"] = runtime_contents() + (f"SCHOLIGHT_ZILLIZ_TOKEN={secret}\n")

    result = run_bootstrap(env, source)

    runtime = Path(env["SCHOLIGHT_BOOTSTRAP_ROOT"]) / "etc" / "scholight" / "runtime.env"
    assert result.returncode != 0
    assert "duplicate SCHOLIGHT_ZILLIZ_TOKEN" in result.stderr
    assert not runtime.exists()
    assert secret not in result.stdout + result.stderr


def test_oversized_parameter_is_rejected_before_install(tmp_path: Path) -> None:
    env, source, _ = bootstrap_environment(tmp_path, None)
    env["FAKE_PARAMETER_VALUE"] = "X" * 4097

    result = run_bootstrap(env, source)

    runtime = Path(env["SCHOLIGHT_BOOTSTRAP_ROOT"]) / "etc" / "scholight" / "runtime.env"
    assert result.returncode != 0
    assert "1-4096 bytes" in result.stderr
    assert not runtime.exists()


def test_existing_runtime_symlink_fails_without_parameter_read(tmp_path: Path) -> None:
    env, source, log = bootstrap_environment(tmp_path, None)
    runtime = Path(env["SCHOLIGHT_BOOTSTRAP_ROOT"]) / "etc" / "scholight" / "runtime.env"
    runtime.parent.mkdir()
    target = tmp_path / "runtime-target.env"
    target.write_text(runtime_contents(), encoding="utf-8")
    target.chmod(0o600)
    runtime.symlink_to(target)

    result = run_bootstrap(env, source)

    commands = log.read_text(encoding="utf-8")
    assert result.returncode != 0
    assert "regular non-symlink" in result.stderr
    assert "aws " not in commands


def test_existing_runtime_with_open_permissions_fails_closed(tmp_path: Path) -> None:
    env, source, _ = bootstrap_environment(tmp_path, runtime_contents())
    runtime = Path(env["SCHOLIGHT_BOOTSTRAP_ROOT"]) / "etc" / "scholight" / "runtime.env"
    runtime.chmod(0o644)

    result = run_bootstrap(env, source)

    assert result.returncode != 0
    assert "mode 0600" in result.stderr


def test_missing_docker_is_installed_before_release(tmp_path: Path) -> None:
    env, source, log = bootstrap_environment(tmp_path, runtime_contents())
    bin_dir = Path(env["PATH"].split(":", 1)[0])
    docker = bin_dir / "docker"
    docker_template = tmp_path / "docker-template"
    docker.rename(docker_template)
    for command in (
        "awk",
        "bash",
        "chmod",
        "cp",
        "dirname",
        "grep",
        "id",
        "install",
        "mktemp",
        "mv",
        "rm",
        "sed",
        "sha256sum",
        "stat",
        "tr",
        "wc",
    ):
        executable = shutil.which(command)
        assert executable is not None
        (bin_dir / command).symlink_to(executable)
    make_executable(
        bin_dir / "dnf",
        "#!/usr/bin/env bash\n"
        'printf \'dnf %s\\n\' "$*" >>"${FAKE_COMMAND_LOG}"\n'
        'cp "${FAKE_DOCKER_TEMPLATE}" "${FAKE_BIN_DIR}/docker"\n'
        'chmod 0755 "${FAKE_BIN_DIR}/docker"\n',
    )
    env["FAKE_DOCKER_TEMPLATE"] = str(docker_template)
    env["FAKE_BIN_DIR"] = str(bin_dir)
    env["PATH"] = str(bin_dir)

    result = run_bootstrap(env, source)

    commands = log.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert "dnf install -y docker" in commands
    assert "systemctl enable --now docker" in commands


def test_package_mismatch_does_not_modify_installed_package(tmp_path: Path) -> None:
    env, source, _ = bootstrap_environment(tmp_path, runtime_contents())
    installed = Path(env["SCHOLIGHT_BOOTSTRAP_ROOT"]) / "opt" / "scholight"
    installed.mkdir(parents=True)
    marker = installed / "marker"
    marker.write_text("keep", encoding="utf-8")

    result = run_bootstrap(env, source, expected_sha="f" * 64)

    assert result.returncode != 0
    assert "package SHA" in result.stderr
    assert marker.read_text(encoding="utf-8") == "keep"


def test_similar_but_wrong_ecr_hostname_is_rejected(tmp_path: Path) -> None:
    env, source, _ = bootstrap_environment(tmp_path, runtime_contents())
    wrong_backend = BACKEND_IMAGE.replace(".dkr.", "Xdkr.", 1)

    result = subprocess.run(
        [
            "bash",
            str(BOOTSTRAP),
            "deploy",
            "--contract-version",
            "1",
            "--package-sha",
            package_sha(source),
            "--release-sha",
            RELEASE_SHA,
            "--backend-image",
            wrong_backend,
            "--frontend-image",
            FRONTEND_IMAGE,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "outside the production ECR repository" in result.stderr


def test_second_bootstrap_skips_package_install_and_keeps_runtime(tmp_path: Path) -> None:
    env, source, log = bootstrap_environment(tmp_path, runtime_contents())

    first = run_bootstrap(env, source)
    installed_release = Path(env["SCHOLIGHT_BOOTSTRAP_ROOT"]) / "opt" / "scholight" / "release.sh"
    first_mtime = installed_release.stat().st_mtime_ns
    second = run_bootstrap(env, source)

    commands = log.read_text(encoding="utf-8")
    assert first.returncode == second.returncode == 0
    assert installed_release.stat().st_mtime_ns == first_mtime
    assert commands.count("release deploy ") == 2
