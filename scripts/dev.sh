#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

env_file=${SCHOLIGHT_LOCAL_ENV_FILE:-.env.local}
if [[ ! -f $env_file ]]; then
  printf 'Create %s from .env.example before starting Scholight locally.\n' "$env_file" >&2
  exit 1
fi

pg_host=$(awk -F= '$1 == "SCHOLIGHT_PG_HOST" { print substr($0, index($0, "=") + 1) }' "$env_file")
pg_port=$(awk -F= '$1 == "SCHOLIGHT_PG_PORT" { print substr($0, index($0, "=") + 1) }' "$env_file")
if [[ $pg_host != 127.0.0.1 || $pg_port != 55432 ]]; then
  printf 'Local Scholight requires PostgreSQL at 127.0.0.1:55432.\n' >&2
  exit 1
fi

export SCHOLIGHT_DISABLE_DOTENV=1
trap 'kill 0' EXIT INT TERM
uv run --env-file "$env_file" python docker/scholight-api/start.py &
uv run --env-file "$env_file" npm --prefix frontend run dev &
wait
