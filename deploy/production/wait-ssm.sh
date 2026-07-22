#!/usr/bin/env bash
set -euo pipefail

COMMAND_ID=${1:?command ID is required}
INSTANCE_ID=${2:?instance ID is required}
TIMEOUT_SECONDS=${SSM_WAIT_TIMEOUT_SECONDS:-1800}
POLL_SECONDS=${SSM_WAIT_POLL_SECONDS:-10}
DEADLINE=$((SECONDS + TIMEOUT_SECONDS))

while ((SECONDS < DEADLINE)); do
  status=$(aws ssm get-command-invocation \
    --command-id "${COMMAND_ID}" \
    --instance-id "${INSTANCE_ID}" \
    --query Status \
    --output text 2>/dev/null || true)
  case ${status} in
    Success)
      aws ssm get-command-invocation \
        --command-id "${COMMAND_ID}" \
        --instance-id "${INSTANCE_ID}"
      exit 0
      ;;
    Cancelled|Cancelling|Failed|TimedOut|Undeliverable|Terminated)
      aws ssm get-command-invocation \
        --command-id "${COMMAND_ID}" \
        --instance-id "${INSTANCE_ID}" || true
      printf 'SSM command %s finished with status %s\n' "${COMMAND_ID}" "${status}" >&2
      exit 1
      ;;
    Pending|InProgress|Delayed|"") ;;
    *)
      printf 'Unexpected SSM command status: %s\n' "${status}" >&2
      exit 1
      ;;
  esac
  sleep "${POLL_SECONDS}"
done

aws ssm cancel-command --command-id "${COMMAND_ID}" --instance-ids "${INSTANCE_ID}" || true
printf 'SSM command %s exceeded %ss and cancellation was requested\n' \
  "${COMMAND_ID}" "${TIMEOUT_SECONDS}" >&2
exit 124
