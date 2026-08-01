#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
COMPOSE_FILE=${SCHOLIGHT_COMPOSE_FILE:-"${SCRIPT_DIR}/compose.yaml"}
RUNTIME_ENV=${SCHOLIGHT_RUNTIME_ENV:-/etc/scholight/runtime.env}
RELEASE_ENV=${SCHOLIGHT_RELEASE_ENV:?SCHOLIGHT_RELEASE_ENV is required}
COMPOSE_COMMAND=${SCHOLIGHT_COMPOSE_COMMAND:-"${SCRIPT_DIR}/compose-command.sh"}
ATTEMPTS=${SCHOLIGHT_SMOKE_ATTEMPTS:-12}
DELAY_SECONDS=${SCHOLIGHT_SMOKE_DELAY_SECONDS:-5}

compose() {
  SCHOLIGHT_RUNTIME_ENV="${RUNTIME_ENV}" SCHOLIGHT_RELEASE_ENV="${RELEASE_ENV}" \
    SCHOLIGHT_COMPOSE_FILE="${COMPOSE_FILE}" "${COMPOSE_COMMAND}" "$@"
}

retry() {
  local description=$1
  shift
  local attempt
  for ((attempt = 1; attempt <= ATTEMPTS; attempt++)); do
    if "$@"; then
      return 0
    fi
    if ((attempt < ATTEMPTS)); then
      sleep "${DELAY_SECONDS}"
    fi
  done
  printf 'Smoke check failed: %s\n' "${description}" >&2
  return 1
}

read_env_value() {
  local key=$1
  local line
  local value=""
  local found=false
  while IFS= read -r line || [[ -n ${line} ]]; do
    [[ ${line} == "${key}="* ]] || continue
    [[ ${found} == false ]] || {
      printf 'Duplicate %s in %s\n' "${key}" "${RUNTIME_ENV}" >&2
      return 1
    }
    value=${line#*=}
    found=true
  done <"${RUNTIME_ENV}"
  [[ ${found} == true && -n ${value} ]] || {
    printf '%s is required in %s\n' "${key}" "${RUNTIME_ENV}" >&2
    return 1
  }
  printf '%s\n' "${value}"
}

service_running() {
  local service=$1
  [[ $(compose ps --services --status running "${service}") == "${service}" ]]
}

SCHOLIGHT_DOMAIN=$(read_env_value SCHOLIGHT_DOMAIN)
SCHOLIGHT_EDGE_DOMAIN=$(read_env_value SCHOLIGHT_EDGE_DOMAIN)
SCHOLIGHT_SURVEY_ENABLED=$(read_env_value SCHOLIGHT_SURVEY_ENABLED)
LOCAL_INGRESS_RESOLVE="${SCHOLIGHT_DOMAIN}:443:127.0.0.1"
LOCAL_EDGE_RESOLVE="${SCHOLIGHT_EDGE_DOMAIN}:80:127.0.0.1"

retry "API readiness" compose exec -T api \
  curl --fail --silent --show-error http://127.0.0.1:8000/readyz
retry "frontend health" compose exec -T frontend \
  wget -q -O /dev/null http://127.0.0.1:8080/healthz
retry "metadata-sync running" service_running metadata-sync
retry "paper-ingest running" service_running paper-ingest
if [[ ${SCHOLIGHT_SURVEY_ENABLED} == true ]]; then
  retry "Survey Draft worker running" service_running survey-draft-worker
  retry "Survey execution worker running" service_running survey-worker
  retry "Survey cleanup sibling heartbeat" compose exec -T survey-worker \
    /bin/sh -ec 'find /tmp/scholight-survey-cleanup.heartbeat -mmin -2 -print -quit | grep -q .'
  retry "Survey migrations, configuration, cleanup, and S3 access" compose exec -T survey-worker \
    /app/.venv/bin/scholight survey smoke --json-output
elif [[ ${SCHOLIGHT_SURVEY_ENABLED} != false ]]; then
  printf 'SCHOLIGHT_SURVEY_ENABLED must be exactly true or false\n' >&2
  exit 1
fi
retry "load balancer origin health" curl --fail --silent --show-error \
  --output /dev/null "http://127.0.0.1/healthz"
retry "edge HTTP API ingress" curl --fail --silent --show-error \
  --resolve "${LOCAL_EDGE_RESOLVE}" \
  --output /dev/null "http://${SCHOLIGHT_EDGE_DOMAIN}/api/openapi.json"
retry "local TLS frontend ingress" curl --fail --silent --show-error \
  --resolve "${LOCAL_INGRESS_RESOLVE}" \
  --output /dev/null "https://${SCHOLIGHT_DOMAIN}/healthz"
retry "local TLS API ingress" curl --fail --silent --show-error \
  --resolve "${LOCAL_INGRESS_RESOLVE}" \
  --output /dev/null "https://${SCHOLIGHT_DOMAIN}/api/openapi.json"
retry "agent discovery document" curl --fail --silent --show-error \
  --resolve "${LOCAL_INGRESS_RESOLVE}" \
  --output /dev/null "https://${SCHOLIGHT_DOMAIN}/llms.txt"
retry "agent integration documentation" curl --fail --silent --show-error \
  --resolve "${LOCAL_INGRESS_RESOLVE}" \
  --output /dev/null "https://${SCHOLIGHT_DOMAIN}/docs.md"

for path in livez readyz; do
  status=$(curl --silent --show-error --resolve "${LOCAL_INGRESS_RESOLVE}" \
    --output /dev/null --write-out '%{http_code}' \
    "https://${SCHOLIGHT_DOMAIN}/api/${path}")
  if [[ ${status} != 404 ]]; then
    printf 'TLS ingress /api/%s returned %s, expected 404\n' "${path}" "${status}" >&2
    exit 1
  fi
done
