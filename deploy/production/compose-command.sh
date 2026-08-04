#!/usr/bin/env bash
set -euo pipefail

scholight_compose() {
  local runtime_env=${SCHOLIGHT_RUNTIME_ENV:?SCHOLIGHT_RUNTIME_ENV is required}
  local release_env=${SCHOLIGHT_RELEASE_ENV:?SCHOLIGHT_RELEASE_ENV is required}
  local compose_file=${SCHOLIGHT_COMPOSE_FILE:?SCHOLIGHT_COMPOSE_FILE is required}
  docker compose --env-file "${runtime_env}" --env-file "${release_env}" \
    -f "${compose_file}" "$@"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  scholight_compose "$@"
fi
