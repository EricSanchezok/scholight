#!/usr/bin/env bash
set -euo pipefail

# Never inherit shell tracing: it could expose the decrypted provider keys.
set +x

readonly AWS_REGION="ap-southeast-1"
readonly RUNTIME_PARAMETER="/scholight/production/runtime-env"

if [[ $# -eq 0 ]]; then
  echo "usage: $0 <command> [args ...]" >&2
  exit 64
fi

if ! command -v aws >/dev/null 2>&1; then
  echo "AWS CLI is required to load the Survey model credentials." >&2
  exit 69
fi

runtime_env="$({
  aws ssm get-parameter \
    --no-cli-pager \
    --region "${AWS_REGION}" \
    --name "${RUNTIME_PARAMETER}" \
    --with-decryption \
    --query Parameter.Value \
    --output text
})"

extract_single_value() {
  local key="$1"
  local line
  local value=""
  local matches=0

  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%$'\r'}"
    if [[ "${line}" == "${key}="* ]]; then
      matches=$((matches + 1))
      value="${line#*=}"
    fi
  done <<<"${runtime_env}"

  if [[ ${matches} -ne 1 || -z "${value}" ]]; then
    echo "${key} must appear exactly once with a non-empty value in ${RUNTIME_PARAMETER}." >&2
    return 1
  fi

  printf '%s' "${value}"
}

DEEPSEEK_API_KEY="$(extract_single_value DEEPSEEK_API_KEY)"
IMAGE_GEN_API_KEY="$(extract_single_value IMAGE_GEN_API_KEY)"
export DEEPSEEK_API_KEY IMAGE_GEN_API_KEY

unset runtime_env
exec "$@"
