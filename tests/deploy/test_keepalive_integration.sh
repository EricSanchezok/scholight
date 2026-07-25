#!/usr/bin/env bash
set -euo pipefail

readonly NETWORK=scholight-keepalive-test
readonly CADDY_IMAGE='docker.io/library/caddy@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d'
readonly PORT=18081

cleanup() {
  docker rm -f keepalive-caddy keepalive-api >/dev/null 2>&1 || true
  docker network rm "${NETWORK}" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

docker network create "${NETWORK}" >/dev/null
docker run -d --rm \
  --name keepalive-api \
  --network "${NETWORK}" \
  --entrypoint /app/.venv/bin/uvicorn \
  -v "${PWD}/tests/deploy/keepalive_app.py:/test/keepalive_app.py:ro" \
  scholight-api:ci \
  keepalive_app:app \
  --app-dir /test \
  --host 0.0.0.0 \
  --port 8000 \
  --timeout-keep-alive 3 >/dev/null
docker run -d --rm \
  --name keepalive-caddy \
  --network "${NETWORK}" \
  -p "127.0.0.1:${PORT}:8080" \
  -v "${PWD}/tests/deploy/keepalive.Caddyfile:/etc/caddy/Caddyfile:ro" \
  "${CADDY_IMAGE}" >/dev/null

for _ in {1..30}; do
  if curl --fail --silent --output /dev/null -X POST "http://127.0.0.1:${PORT}/search"; then
    break
  fi
  sleep 0.2
done

python3 tests/deploy/keepalive_probe.py "http://127.0.0.1:${PORT}/search"
