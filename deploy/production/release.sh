#!/usr/bin/env bash
set -euo pipefail

readonly CONTRACT_VERSION=1
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
COMPOSE_FILE=${SCHOLIGHT_COMPOSE_FILE:-"${SCRIPT_DIR}/compose.yaml"}
SMOKE_SCRIPT=${SCHOLIGHT_SMOKE_SCRIPT:-"${SCRIPT_DIR}/smoke.sh"}
RUNTIME_ENV=${SCHOLIGHT_RUNTIME_ENV:-/etc/scholight/runtime.env}
STATE_DIR=${SCHOLIGHT_STATE_DIR:-/var/lib/scholight}
CURRENT_ENV="${STATE_DIR}/current.env"
PREVIOUS_ENV="${STATE_DIR}/previous.env"
LOCK_FILE="${STATE_DIR}/deploy.lock"
FAILED_DIR="${STATE_DIR}/failed"
TRANSITION_ENV="${STATE_DIR}/transition.env"
CANDIDATE_ENV=""

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

read_env_value() {
  local key=$1
  local line
  local value=""
  local found=false
  while IFS= read -r line || [[ -n ${line} ]]; do
    [[ ${line} == "${key}="* ]] || continue
    [[ ${found} == false ]] || fail "duplicate ${key} in ${RUNTIME_ENV}"
    value=${line#*=}
    found=true
  done <"${RUNTIME_ENV}"
  [[ ${found} == true && -n ${value} ]] || fail "${key} is required in ${RUNTIME_ENV}"
  printf '%s\n' "${value}"
}

read_file_value() {
  local path=$1
  local key=$2
  local line
  local value=""
  local found=false
  while IFS= read -r line || [[ -n ${line} ]]; do
    [[ ${line} == "${key}="* ]] || continue
    [[ ${found} == false ]] || fail "duplicate ${key} in ${path}"
    value=${line#*=}
    found=true
  done <"${path}"
  [[ ${found} == true && -n ${value} ]] || fail "${key} is required in ${path}"
  printf '%s\n' "${value}"
}

file_mode() {
  stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"
}

file_owner_uid() {
  stat -c '%u' "$1" 2>/dev/null || stat -f '%u' "$1"
}

validate_runtime_env() {
  [[ -f ${RUNTIME_ENV} && ! -L ${RUNTIME_ENV} ]] || \
    fail "runtime env must be a regular non-symlink file: ${RUNTIME_ENV}"
  local mode
  mode=$(file_mode "${RUNTIME_ENV}")
  [[ ${mode} == 600 ]] || fail "runtime env must have mode 0600: ${RUNTIME_ENV}"
  local owner_uid
  owner_uid=$(file_owner_uid "${RUNTIME_ENV}")
  [[ ${owner_uid} == 0 || ${owner_uid} == "$(id -u)" ]] || \
    fail "runtime env must be owned by root or the deployment user: ${RUNTIME_ENV}"
}

validate_digest_reference() {
  [[ $1 =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] || \
    fail "image must be a digest-qualified reference: $1"
}

validate_release_sha() {
  [[ $1 =~ ^[0-9a-f]{40}$ ]] || fail "release SHA must contain 40 lowercase hex characters"
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

package_sha() {
  local inventory=""
  local path
  for path in "${SCRIPT_DIR}/compose.yaml" "${SCRIPT_DIR}/Caddyfile" \
    "${SCRIPT_DIR}/cloudwatch-agent.json" \
    "${SCRIPT_DIR}/bootstrap-db.sql" "${SCRIPT_DIR}/bootstrap.sh" \
    "${SCRIPT_DIR}/release.sh" \
    "${SCRIPT_DIR}/smoke.sh" "${SCRIPT_DIR}/wait-ssm.sh"; do
    [[ -f ${path} && ! -L ${path} ]] || fail "production package file missing: ${path}"
    inventory+="${path##*/}:$(sha256_file "${path}")"$'\n'
  done
  sha256_text "${inventory}"
}

compose() {
  local release_env=$1
  shift
  docker compose --env-file "${RUNTIME_ENV}" --env-file "${release_env}" \
    -f "${COMPOSE_FILE}" "$@"
}

acquire_lock() {
  require_command flock
  mkdir -p "${STATE_DIR}" "${FAILED_DIR}"
  exec 9>"${LOCK_FILE}"
  flock -n 9 || fail "another Scholight release operation is running"
}

ensure_no_unfinished_transition() {
  [[ ! -e ${TRANSITION_ENV} ]] || \
    fail "unfinished release transition requires reconciliation: ${TRANSITION_ENV}"
}

write_transition() {
  local operation=$1
  local stage=$2
  local target=$3
  local transition_tmp="${TRANSITION_ENV}.tmp"
  umask 077
  {
    printf 'SCHOLIGHT_TRANSITION_OPERATION=%s\n' "${operation}"
    printf 'SCHOLIGHT_TRANSITION_STAGE=%s\n' "${stage}"
    printf 'SCHOLIGHT_TRANSITION_TARGET=%s\n' "${target}"
  } >"${transition_tmp}"
  mv "${transition_tmp}" "${TRANSITION_ENV}"
}

clear_transition() {
  rm -f "${TRANSITION_ENV}"
}

cleanup_candidate() {
  if [[ -n ${CANDIDATE_ENV} ]]; then
    rm -f -- "${CANDIDATE_ENV}"
  fi
}

write_manifest() {
  local destination=$1
  local package_digest=$2
  local release_sha=$3
  local backend_image=$4
  local frontend_image=$5
  umask 077
  {
    printf 'SCHOLIGHT_RELEASE_CONTRACT_VERSION=%s\n' "${CONTRACT_VERSION}"
    printf 'SCHOLIGHT_PACKAGE_SHA=%s\n' "${package_digest}"
    printf 'SCHOLIGHT_RELEASE_SHA=%s\n' "${release_sha}"
    printf 'SCHOLIGHT_BACKEND_IMAGE=%s\n' "${backend_image}"
    printf 'SCHOLIGHT_FRONTEND_IMAGE=%s\n' "${frontend_image}"
  } >"${destination}"
}

registry_from_image() {
  printf '%s\n' "${1%%/*}"
}

ecr_login() {
  local backend_image=$1
  local frontend_image=$2
  local backend_registry
  local frontend_registry
  backend_registry=$(registry_from_image "${backend_image}")
  frontend_registry=$(registry_from_image "${frontend_image}")
  local aws_region
  local ecr_registry
  aws_region=$(read_env_value SCHOLIGHT_AWS_REGION)
  ecr_registry=$(read_env_value SCHOLIGHT_ECR_REGISTRY)
  [[ ${backend_registry} == "${ecr_registry}" ]] || \
    fail "backend image is not hosted in SCHOLIGHT_ECR_REGISTRY"
  [[ ${frontend_registry} == "${ecr_registry}" ]] || \
    fail "frontend image is not hosted in SCHOLIGHT_ECR_REGISTRY"
  aws ecr get-login-password --region "${aws_region}" | \
    docker login --username AWS --password-stdin "${ecr_registry}"
}

capture_diagnostics() {
  local release_env=$1
  local destination=$2
  mkdir -p "${destination}"
  compose "${release_env}" ps --all >"${destination}/compose-ps.txt" 2>&1 || true
  compose "${release_env}" logs --no-color --tail 500 >"${destination}/compose-logs.txt" 2>&1 || true
}

activate() {
  local release_env=$1
  # Compose's parallel in-place recreation can race while renaming temporary
  # containers to their canonical names. A coordinated single-host release has
  # an accepted short maintenance window, so remove the existing project first.
  # Named volumes are preserved because `-v` is intentionally not used.
  compose "${release_env}" down --remove-orphans --timeout 30
  compose "${release_env}" up -d --no-build --remove-orphans
  SCHOLIGHT_RELEASE_ENV="${release_env}" "${SMOKE_SCRIPT}"
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
  validate_release_sha "${release_sha}"
  validate_digest_reference "${backend_image}"
  validate_digest_reference "${frontend_image}"
  [[ ${expected_package_sha} =~ ^[0-9a-f]{64}$ ]] || \
    fail "package SHA must contain 64 lowercase hex characters"
  local installed_package_sha
  installed_package_sha=$(package_sha)
  [[ ${expected_package_sha} == "${installed_package_sha}" ]] || \
    fail "installed production package does not match release package SHA"
  validate_runtime_env
  require_command aws
  require_command docker
  acquire_lock
  ensure_no_unfinished_transition

  local candidate
  candidate=$(mktemp "${STATE_DIR}/candidate.XXXXXX.env")
  write_manifest "${candidate}" "${installed_package_sha}" "${release_sha}" "${backend_image}" "${frontend_image}"
  CANDIDATE_ENV=${candidate}
  trap cleanup_candidate EXIT

  compose "${candidate}" config --quiet
  ecr_login "${backend_image}" "${frontend_image}"
  compose "${candidate}" pull api frontend
  compose "${candidate}" --profile migrate run --rm migrate

  local current_snapshot=""
  if [[ -f ${CURRENT_ENV} ]]; then
    current_snapshot=$(mktemp "${STATE_DIR}/current-snapshot.XXXXXX.env")
    cp "${CURRENT_ENV}" "${current_snapshot}"
  fi

  write_transition deploy activating "${candidate}"
  if ! activate "${candidate}"; then
    local failed_release="${FAILED_DIR}/${release_sha}"
    capture_diagnostics "${candidate}" "${failed_release}"
    cp "${candidate}" "${failed_release}/release.env"
    if [[ -n ${current_snapshot} ]]; then
      if ! activate "${current_snapshot}"; then
        fail "candidate failed and current release smoke check also failed"
      fi
    else
      compose "${candidate}" down --remove-orphans || true
    fi
    clear_transition
    rm -f "${current_snapshot}"
    fail "candidate release failed smoke checks; current release restored when available"
  fi
  write_transition deploy activated "${candidate}"

  if [[ -n ${current_snapshot} ]]; then
    mv "${current_snapshot}" "${PREVIOUS_ENV}"
  fi
  mv "${candidate}" "${CURRENT_ENV}"
  CANDIDATE_ENV=""
  clear_transition
  trap - EXIT
  printf 'Deployed Scholight release %s\n' "${release_sha}"
}

rollback() {
  [[ -f ${PREVIOUS_ENV} ]] || fail "previous release manifest not found"
  validate_runtime_env
  require_command docker
  acquire_lock
  ensure_no_unfinished_transition
  require_command aws
  local previous_backend_image
  local previous_frontend_image
  previous_backend_image=$(read_file_value "${PREVIOUS_ENV}" SCHOLIGHT_BACKEND_IMAGE)
  previous_frontend_image=$(read_file_value "${PREVIOUS_ENV}" SCHOLIGHT_FRONTEND_IMAGE)
  validate_digest_reference "${previous_backend_image}"
  validate_digest_reference "${previous_frontend_image}"
  ecr_login "${previous_backend_image}" "${previous_frontend_image}"
  compose "${PREVIOUS_ENV}" pull api frontend

  local old_current="${STATE_DIR}/rollback-current.env"
  cp "${CURRENT_ENV}" "${old_current}"
  write_transition rollback activating "${PREVIOUS_ENV}"
  if ! activate "${PREVIOUS_ENV}"; then
    activate "${old_current}" || fail "rollback and recovery smoke checks both failed"
    rm -f "${old_current}"
    clear_transition
    fail "previous release failed smoke checks; current release restored"
  fi
  write_transition rollback activated "${PREVIOUS_ENV}"
  mv "${PREVIOUS_ENV}" "${CURRENT_ENV}"
  mv "${old_current}" "${PREVIOUS_ENV}"
  clear_transition
  printf 'Rolled back Scholight to the previous coordinated release\n'
}

status() {
  if [[ -e ${TRANSITION_ENV} ]]; then
    printf 'Unfinished transition requires reconciliation:\n'
    sed -n '/^SCHOLIGHT_TRANSITION_/p' "${TRANSITION_ENV}"
    return 1
  fi
  if [[ -f ${CURRENT_ENV} ]]; then
    printf 'Current release:\n'
    sed -n '/^SCHOLIGHT_RELEASE_\|^SCHOLIGHT_.*_IMAGE=/p' "${CURRENT_ENV}"
  else
    printf 'No current release recorded.\n'
  fi
  if [[ -f ${PREVIOUS_ENV} ]]; then
    printf 'Previous release is available.\n'
  fi
}

case ${1:-} in
  deploy) shift; deploy "$@" ;;
  rollback) shift; rollback "$@" ;;
  status) status ;;
  package-sha) package_sha ;;
  *) fail "usage: release.sh deploy --contract-version ${CONTRACT_VERSION} --package-sha SHA256 --release-sha SHA --backend-image IMAGE@DIGEST --frontend-image IMAGE@DIGEST | rollback | status | package-sha" ;;
esac
