#!/usr/bin/env bash
set -euo pipefail

readonly CONTRACT_VERSION=1
readonly COMPOSE_VERSION=2.40.3
readonly COMPOSE_SHA256=dba9d98e1ba5bfe11d88c99b9bd32fc4a0624a30fafe68eea34d61a3e42fd372
readonly COMPOSE_URL="https://github.com/docker/compose/releases/download/v${COMPOSE_VERSION}/docker-compose-linux-x86_64"
readonly RUNTIME_PARAMETER=/scholight/production/runtime-env
readonly MAX_PARAMETER_BYTES=4096
readonly AWS_REGION=ap-southeast-1
readonly ECR_REGISTRY=683390797772.dkr.ecr.ap-southeast-1.amazonaws.com
readonly BACKEND_IMAGE_REGEX='^683390797772\.dkr\.ecr\.ap-southeast-1\.amazonaws\.com/scholight/backend@sha256:[0-9a-f]{64}$'
readonly FRONTEND_IMAGE_REGEX='^683390797772\.dkr\.ecr\.ap-southeast-1\.amazonaws\.com/scholight/frontend@sha256:[0-9a-f]{64}$'
readonly SWAP_BYTES=2147483648

ROOT_PREFIX=${SCHOLIGHT_BOOTSTRAP_ROOT:-}
if [[ -n ${ROOT_PREFIX} ]]; then
  [[ ${ROOT_PREFIX} == /* && ${ROOT_PREFIX} != / ]] || {
    printf 'Error: SCHOLIGHT_BOOTSTRAP_ROOT must be an absolute non-root test path\n' >&2
    exit 1
  }
else
  [[ $(id -u) == 0 ]] || {
    printf 'Error: bootstrap must run as root\n' >&2
    exit 1
  }
fi

readonly HOST_PACKAGE_DIR="${ROOT_PREFIX}/opt/scholight"
readonly CONFIG_DIR="${ROOT_PREFIX}/etc/scholight"
readonly STATE_DIR="${ROOT_PREFIX}/var/lib/scholight"
readonly RUNTIME_ENV="${CONFIG_DIR}/runtime.env"
readonly COMPOSE_PLUGIN="${ROOT_PREFIX}/usr/local/lib/docker/cli-plugins/docker-compose"
readonly OS_RELEASE="${ROOT_PREFIX}/etc/os-release"
readonly SOURCE_PACKAGE_DIR=${SCHOLIGHT_SOURCE_PACKAGE_DIR:-/opt/scholight-package}
readonly LOCK_FILE="${STATE_DIR}/bootstrap.lock"
readonly SWAP_FILE="${ROOT_PREFIX}/swapfile"
readonly FSTAB="${ROOT_PREFIX}/etc/fstab"
readonly SWAPPINESS_CONFIG="${ROOT_PREFIX}/etc/sysctl.d/99-scholight.conf"
readonly PACKAGE_FILES=(
  compose.yaml
  Caddyfile
  cloudwatch-agent.json
  bootstrap-db.sql
  bootstrap.sh
  release.sh
  smoke.sh
  wait-ssm.sh
)
readonly REQUIRED_RUNTIME_KEYS=(
  SCHOLIGHT_DOMAIN
  SCHOLIGHT_ACME_EMAIL
  SCHOLIGHT_AWS_REGION
  SCHOLIGHT_ECR_REGISTRY
  SCHOLIGHT_PG_HOST
  SCHOLIGHT_PG_DATABASE
  SCHOLIGHT_APP_PG_USER
  SCHOLIGHT_APP_PG_PASSWORD
  SCHOLIGHT_MIGRATION_PG_USER
  SCHOLIGHT_MIGRATION_PG_PASSWORD
  SCHOLIGHT_ZILLIZ_URI
  SCHOLIGHT_ZILLIZ_TOKEN
  SCHOLIGHT_EMBEDDING_BASE_URL
  SCHOLIGHT_EMBEDDING_API_KEY
  SCHOLIGHT_EMBEDDING_MODEL
  SCHOLIGHT_AUTH_JWT_SECRET
  SCHOLIGHT_ANONYMOUS_QUOTA_HMAC_SECRET
  SCHOLIGHT_ACCESS_KEY_HMAC_SECRET
  SCHOLIGHT_PUBLIC_WEB_URL
  SCHOLIGHT_CORS_ALLOW_ORIGINS
)
TEMP_PATHS=()

cleanup_temps() {
  local path
  for path in ${TEMP_PATHS[@]+"${TEMP_PATHS[@]}"}; do
    rm -rf -- "${path}"
  done
}
trap cleanup_temps EXIT

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

sha256_file() {
  local path=$1
  local output
  if command -v sha256sum >/dev/null 2>&1; then
    output=$(sha256sum "${path}")
  else
    require_command shasum
    output=$(shasum -a 256 "${path}")
  fi
  printf '%s\n' "${output%% *}"
}

sha256_text() {
  local value=$1
  local output
  if command -v sha256sum >/dev/null 2>&1; then
    output=$(printf '%s' "${value}" | sha256sum)
  else
    require_command shasum
    output=$(printf '%s' "${value}" | shasum -a 256)
  fi
  printf '%s\n' "${output%% *}"
}

package_sha() {
  local directory=$1
  local inventory=""
  local name
  local path
  for name in "${PACKAGE_FILES[@]}"; do
    path="${directory}/${name}"
    [[ -f ${path} && ! -L ${path} ]] || fail "production package file missing: ${path}"
    inventory+="${name}:$(sha256_file "${path}")"$'\n'
  done
  sha256_text "${inventory}"
}

package_is_complete() {
  local directory=$1
  local name
  for name in "${PACKAGE_FILES[@]}"; do
    [[ -f ${directory}/${name} && ! -L ${directory}/${name} ]] || return 1
  done
}

file_mode() {
  stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"
}

file_owner_uid() {
  stat -c '%u' "$1" 2>/dev/null || stat -f '%u' "$1"
}

file_size() {
  stat -c '%s' "$1" 2>/dev/null || stat -f '%z' "$1"
}

expected_owner_uid() {
  if [[ -n ${ROOT_PREFIX} ]]; then
    id -u
  else
    printf '0\n'
  fi
}

validate_platform() {
  [[ $(uname -m) == x86_64 ]] || fail "bootstrap supports only x86_64"
  local os_release_path=${OS_RELEASE}
  if [[ -L ${OS_RELEASE} ]]; then
    [[ $(readlink "${OS_RELEASE}") == ../usr/lib/os-release ]] || \
      fail "operating system metadata has an unexpected symlink target: ${OS_RELEASE}"
    os_release_path="${ROOT_PREFIX}/usr/lib/os-release"
  fi
  [[ -f ${os_release_path} && ! -L ${os_release_path} ]] || \
    fail "operating system metadata is missing: ${os_release_path}"
  local os_id
  local version_id
  os_id=$(sed -n 's/^ID=//p' "${os_release_path}" | tr -d '"')
  version_id=$(sed -n 's/^VERSION_ID=//p' "${os_release_path}" | tr -d '"')
  [[ ${os_id} == amzn && ${version_id} == 2023 ]] || \
    fail "bootstrap supports only Amazon Linux 2023"
}

acquire_lock() {
  require_command flock
  install -d -m 0700 "${STATE_DIR}"
  exec 9>"${LOCK_FILE}"
  flock -n 9 || fail "another Scholight bootstrap is running"
}

ensure_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    require_command dnf
    dnf install -y docker
  fi
  require_command systemctl
  systemctl enable --now docker
  require_command docker
}

ensure_compose() {
  local installed_version=""
  installed_version=$(docker compose version --short 2>/dev/null || true)
  installed_version=${installed_version#v}
  if [[ ${installed_version} == "${COMPOSE_VERSION}" ]]; then
    return
  fi

  require_command curl
  local plugin_tmp
  plugin_tmp=$(mktemp "${STATE_DIR}/docker-compose.XXXXXX")
  TEMP_PATHS+=("${plugin_tmp}")
  curl --fail --location --silent --show-error --output "${plugin_tmp}" "${COMPOSE_URL}"
  [[ $(sha256_file "${plugin_tmp}") == "${COMPOSE_SHA256}" ]] || \
    fail "Docker Compose checksum verification failed"
  install -d -m 0755 "$(dirname "${COMPOSE_PLUGIN}")"
  rm -f "${COMPOSE_PLUGIN}"
  install -m 0755 "${plugin_tmp}" "${COMPOSE_PLUGIN}"
  [[ -f ${COMPOSE_PLUGIN} && ! -L ${COMPOSE_PLUGIN} ]] || \
    fail "Docker Compose plugin must be a regular non-symlink file"
  [[ $(sha256_file "${COMPOSE_PLUGIN}") == "${COMPOSE_SHA256}" ]] || \
    fail "installed Docker Compose checksum verification failed"
  rm -f "${plugin_tmp}"

  installed_version=$(docker compose version --short 2>/dev/null || true)
  installed_version=${installed_version#v}
  [[ ${installed_version} == "${COMPOSE_VERSION}" ]] || \
    fail "Docker Compose ${COMPOSE_VERSION} is not available after installation"
}

ensure_directories() {
  install -d -m 0755 "${HOST_PACKAGE_DIR}"
  install -d -m 0750 "${CONFIG_DIR}"
  install -d -m 0700 "${STATE_DIR}"
  chmod 0755 "${HOST_PACKAGE_DIR}"
  chmod 0750 "${CONFIG_DIR}"
  chmod 0700 "${STATE_DIR}"
  if [[ -z ${ROOT_PREFIX} ]]; then
    chown root:root "${HOST_PACKAGE_DIR}" "${CONFIG_DIR}" "${STATE_DIR}"
  fi
}

ensure_observability() {
  if [[ -n ${ROOT_PREFIX} ]]; then
    return
  fi
  if ! command -v rsyslogd >/dev/null 2>&1 || \
    [[ ! -x /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl ]]; then
    require_command dnf
    dnf install -y rsyslog amazon-cloudwatch-agent
  fi
  systemctl enable --now rsyslog
  /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
    -a fetch-config \
    -m ec2 \
    -s \
    -c "file:${HOST_PACKAGE_DIR}/cloudwatch-agent.json"
}

ensure_emergency_swap() {
  require_command swapon
  local active_swap
  active_swap=$(swapon --show=NAME --noheadings 2>/dev/null || true)

  if [[ ! -f ${SWAP_FILE} || -L ${SWAP_FILE} ]] || \
    [[ $(file_size "${SWAP_FILE}" 2>/dev/null || printf '0') != "${SWAP_BYTES}" ]]; then
    ! grep -Fxq "${SWAP_FILE}" <<<"${active_swap}" || \
      fail "active Scholight swap has an unexpected size"
    require_command fallocate
    require_command mkswap
    rm -f -- "${SWAP_FILE}"
    fallocate -l 2G "${SWAP_FILE}"
    chmod 0600 "${SWAP_FILE}"
    mkswap "${SWAP_FILE}" >/dev/null
  fi
  [[ -f ${SWAP_FILE} && ! -L ${SWAP_FILE} ]] || fail "swap must be a regular file"
  [[ $(file_mode "${SWAP_FILE}") == 600 ]] || fail "swap must have mode 0600"

  install -d -m 0755 "$(dirname "${FSTAB}")" "$(dirname "${SWAPPINESS_CONFIG}")"
  touch "${FSTAB}"
  local swap_entry="${SWAP_FILE} none swap sw 0 0"
  if ! grep -Fxq "${swap_entry}" "${FSTAB}"; then
    printf '%s\n' "${swap_entry}" >>"${FSTAB}"
  fi
  printf 'vm.swappiness=10\n' >"${SWAPPINESS_CONFIG}.new"
  chmod 0644 "${SWAPPINESS_CONFIG}.new"
  mv -f "${SWAPPINESS_CONFIG}.new" "${SWAPPINESS_CONFIG}"

  if ! grep -Fxq "${SWAP_FILE}" <<<"${active_swap}"; then
    swapon "${SWAP_FILE}"
  fi
  require_command sysctl
  sysctl -w vm.swappiness=10 >/dev/null
}

validate_runtime_contents() {
  local path=$1
  local bytes
  bytes=$(wc -c <"${path}")
  ((bytes > 0 && bytes <= MAX_PARAMETER_BYTES)) || \
    fail "runtime env must contain 1-${MAX_PARAMETER_BYTES} bytes"
  if LC_ALL=C grep -q $'\r' "${path}"; then
    fail "runtime env must use Unix line endings"
  fi

  local invalid_line
  invalid_line=$(awk '
    /^[[:space:]]*$/ || /^[[:space:]]*#/ { next }
    /^[A-Z_][A-Z0-9_]*=/ { next }
    { print NR; exit }
  ' "${path}")
  [[ -z ${invalid_line} ]] || fail "runtime env contains an invalid line"

  local duplicate_key
  duplicate_key=$(awk -F= '
    /^[A-Z_][A-Z0-9_]*=/ {
      if (seen[$1]++) {
        print $1
        exit
      }
    }
  ' "${path}")
  [[ -z ${duplicate_key} ]] || fail "duplicate ${duplicate_key} in runtime env"

  local key
  local value
  for key in "${REQUIRED_RUNTIME_KEYS[@]}"; do
    value=$(awk -F= -v key="${key}" '$1 == key { sub(/^[^=]*=/, ""); print; exit }' "${path}")
    [[ -n ${value} ]] || fail "${key} is required in runtime env"
  done
  value=$(awk -F= '$1 == "SCHOLIGHT_AWS_REGION" { print $2; exit }' "${path}")
  [[ ${value} == "${AWS_REGION}" ]] || \
    fail "SCHOLIGHT_AWS_REGION must be ${AWS_REGION} in runtime env"
  value=$(awk -F= '$1 == "SCHOLIGHT_ECR_REGISTRY" { print $2; exit }' "${path}")
  [[ ${value} == "${ECR_REGISTRY}" ]] || \
    fail "SCHOLIGHT_ECR_REGISTRY must be the production registry in runtime env"
}

validate_existing_runtime() {
  [[ -f ${RUNTIME_ENV} && ! -L ${RUNTIME_ENV} ]] || \
    fail "runtime env must be a regular non-symlink file: ${RUNTIME_ENV}"
  [[ $(file_mode "${RUNTIME_ENV}") == 600 ]] || \
    fail "runtime env must have mode 0600: ${RUNTIME_ENV}"
  [[ $(file_owner_uid "${RUNTIME_ENV}") == "$(expected_owner_uid)" ]] || \
    fail "runtime env must be owned by root: ${RUNTIME_ENV}"
}

write_release_manifest() {
  local path=$1
  local package_digest=$2
  local release_sha=$3
  local backend_image=$4
  local frontend_image=$5
  {
    printf 'SCHOLIGHT_RELEASE_CONTRACT_VERSION=%s\n' "${CONTRACT_VERSION}"
    printf 'SCHOLIGHT_PACKAGE_SHA=%s\n' "${package_digest}"
    printf 'SCHOLIGHT_RELEASE_SHA=%s\n' "${release_sha}"
    printf 'SCHOLIGHT_BACKEND_IMAGE=%s\n' "${backend_image}"
    printf 'SCHOLIGHT_FRONTEND_IMAGE=%s\n' "${frontend_image}"
  } >"${path}"
}

validate_compose_config() {
  local runtime_candidate=$1
  local package_directory=$2
  local package_digest=$3
  local release_sha=$4
  local backend_image=$5
  local frontend_image=$6
  local release_env
  release_env=$(mktemp "${STATE_DIR}/release.XXXXXX.env")
  TEMP_PATHS+=("${release_env}")
  write_release_manifest \
    "${release_env}" "${package_digest}" "${release_sha}" "${backend_image}" "${frontend_image}"
  docker compose --env-file "${runtime_candidate}" --env-file "${release_env}" \
    -f "${package_directory}/compose.yaml" config --quiet
  rm -f "${release_env}"
}

ensure_runtime_env() {
  local package_directory=$1
  local package_digest=$2
  local release_sha=$3
  local backend_image=$4
  local frontend_image=$5
  if [[ -e ${RUNTIME_ENV} || -L ${RUNTIME_ENV} ]]; then
    validate_existing_runtime
    return
  fi

  require_command aws
  local candidate
  local parameter_value
  candidate=$(mktemp "${STATE_DIR}/runtime.XXXXXX.env")
  TEMP_PATHS+=("${candidate}")
  chmod 0600 "${candidate}"
  parameter_value=$(aws ssm get-parameter \
    --name "${RUNTIME_PARAMETER}" \
    --with-decryption \
    --query Parameter.Value \
    --output text \
    --region "${AWS_REGION}")
  printf '%s' "${parameter_value}" >"${candidate}"
  unset parameter_value
  validate_runtime_contents "${candidate}"
  validate_compose_config \
    "${candidate}" "${package_directory}" "${package_digest}" \
    "${release_sha}" "${backend_image}" "${frontend_image}"

  local destination_tmp="${RUNTIME_ENV}.new.$$"
  TEMP_PATHS+=("${destination_tmp}")
  install -m 0600 "${candidate}" "${destination_tmp}"
  mv -f "${destination_tmp}" "${RUNTIME_ENV}"
  rm -f "${candidate}"
}

install_package() {
  local source_digest=$1
  local installed_digest=""
  if package_is_complete "${HOST_PACKAGE_DIR}"; then
    installed_digest=$(package_sha "${HOST_PACKAGE_DIR}")
  fi
  if [[ ${installed_digest} == "${source_digest}" ]]; then
    return
  fi

  local staging
  staging=$(mktemp -d "${STATE_DIR}/package.XXXXXX")
  TEMP_PATHS+=("${staging}")
  local name
  for name in "${PACKAGE_FILES[@]}"; do
    case ${name} in
      bootstrap.sh | release.sh | smoke.sh | wait-ssm.sh)
        install -m 0755 "${SOURCE_PACKAGE_DIR}/${name}" "${staging}/${name}"
        ;;
      bootstrap-db.sql)
        install -m 0600 "${SOURCE_PACKAGE_DIR}/${name}" "${staging}/${name}"
        ;;
      *)
        install -m 0644 "${SOURCE_PACKAGE_DIR}/${name}" "${staging}/${name}"
        ;;
    esac
  done
  [[ $(package_sha "${staging}") == "${source_digest}" ]] || \
    fail "staged production package does not match package SHA"
  for name in "${PACKAGE_FILES[@]}"; do
    install -m "$(file_mode "${staging}/${name}")" \
      "${staging}/${name}" "${HOST_PACKAGE_DIR}/${name}"
  done
  rm -rf "${staging}"
  [[ $(package_sha "${HOST_PACKAGE_DIR}") == "${source_digest}" ]] || \
    fail "installed production package does not match package SHA"
}

deploy() {
  local contract_version=""
  local expected_package_sha=""
  local release_sha=""
  local backend_image=""
  local frontend_image=""
  while (($#)); do
    case $1 in
      --contract-version) contract_version=${2:-}; shift 2 ;;
      --package-sha) expected_package_sha=${2:-}; shift 2 ;;
      --release-sha) release_sha=${2:-}; shift 2 ;;
      --backend-image) backend_image=${2:-}; shift 2 ;;
      --frontend-image) frontend_image=${2:-}; shift 2 ;;
      *) fail "unknown deploy argument: $1" ;;
    esac
  done

  [[ ${contract_version} == "${CONTRACT_VERSION}" ]] || \
    fail "release contract version must be ${CONTRACT_VERSION}"
  [[ ${expected_package_sha} =~ ^[0-9a-f]{64}$ ]] || \
    fail "package SHA must contain 64 lowercase hex characters"
  [[ ${release_sha} =~ ^[0-9a-f]{40}$ ]] || \
    fail "release SHA must contain 40 lowercase hex characters"
  [[ ${backend_image} =~ ${BACKEND_IMAGE_REGEX} ]] || \
    fail "backend image is outside the production ECR repository"
  [[ ${frontend_image} =~ ${FRONTEND_IMAGE_REGEX} ]] || \
    fail "frontend image is outside the production ECR repository"

  validate_platform
  acquire_lock
  ensure_docker
  ensure_compose
  ensure_directories
  ensure_emergency_swap

  local source_digest
  source_digest=$(package_sha "${SOURCE_PACKAGE_DIR}")
  [[ ${source_digest} == "${expected_package_sha}" ]] || \
    fail "source production package does not match package SHA"
  install_package "${source_digest}"
  ensure_observability
  ensure_runtime_env \
    "${HOST_PACKAGE_DIR}" "${source_digest}" "${release_sha}" \
    "${backend_image}" "${frontend_image}"

  SCHOLIGHT_RUNTIME_ENV="${RUNTIME_ENV}" SCHOLIGHT_STATE_DIR="${STATE_DIR}" \
    "${HOST_PACKAGE_DIR}/release.sh" deploy \
      --contract-version "${CONTRACT_VERSION}" \
      --package-sha "${source_digest}" \
      --release-sha "${release_sha}" \
      --backend-image "${backend_image}" \
      --frontend-image "${frontend_image}"
}

case ${1:-} in
  deploy) shift; deploy "$@" ;;
  *) fail "usage: bootstrap.sh deploy --contract-version 1 --package-sha SHA --release-sha SHA --backend-image IMAGE@DIGEST --frontend-image IMAGE@DIGEST" ;;
esac
