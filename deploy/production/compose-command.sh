#!/usr/bin/env bash
set -euo pipefail

# One Compose entry point keeps deploy, rollback, diagnostics, and smoke on the
# same Survey profile decision. Callers provide the runtime and release files.

scholight_compose_env_value() {
  local path=$1
  local key=$2
  local line
  local value=""
  local found=false
  while IFS= read -r line || [[ -n ${line} ]]; do
    [[ ${line} == "${key}="* ]] || continue
    [[ ${found} == false ]] || {
      printf 'Duplicate %s in %s\n' "${key}" "${path}" >&2
      return 1
    }
    value=${line#*=}
    found=true
  done <"${path}"
  [[ ${found} == true ]] || {
    printf '%s is required in %s\n' "${key}" "${path}" >&2
    return 1
  }
  printf '%s\n' "${value}"
}

scholight_compose() {
  local runtime_env=${SCHOLIGHT_RUNTIME_ENV:?SCHOLIGHT_RUNTIME_ENV is required}
  local release_env=${SCHOLIGHT_RELEASE_ENV:?SCHOLIGHT_RELEASE_ENV is required}
  local compose_file=${SCHOLIGHT_COMPOSE_FILE:?SCHOLIGHT_COMPOSE_FILE is required}
  local enabled
  enabled=$(scholight_compose_env_value "${runtime_env}" SCHOLIGHT_SURVEY_ENABLED)
  case ${enabled} in
    true)
      docker compose --env-file "${runtime_env}" --env-file "${release_env}" \
        -f "${compose_file}" --profile survey "$@"
      ;;
    false)
      docker compose --env-file "${runtime_env}" --env-file "${release_env}" \
        -f "${compose_file}" "$@"
      ;;
    *)
      printf 'SCHOLIGHT_SURVEY_ENABLED must be exactly true or false in %s\n' \
        "${runtime_env}" >&2
      return 1
      ;;
  esac
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  scholight_compose "$@"
fi
