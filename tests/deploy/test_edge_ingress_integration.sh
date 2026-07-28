#!/usr/bin/env bash
set -euo pipefail

readonly NETWORK=scholight-edge-ingress-test
readonly CADDY_IMAGE='docker.io/library/caddy@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d'
readonly PORT=18082
readonly CANONICAL_DOMAIN=scholight.example.invalid
readonly EDGE_DOMAIN=edge.example.invalid

cleanup() {
  docker rm -f edge-ingress-caddy edge-ingress-api edge-ingress-frontend \
    >/dev/null 2>&1 || true
  docker network rm "${NETWORK}" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

assert_status() {
  local expected=$1
  local host=$2
  local request_path=$3
  local actual
  actual=$(curl --silent --output /dev/null --write-out '%{http_code}' \
    --header "Host: ${host}" "http://127.0.0.1:${PORT}${request_path}")
  [[ ${actual} == "${expected}" ]] || {
    printf 'Expected %s for Host %s path %s, got %s\n' \
      "${expected}" "${host}" "${request_path}" "${actual}" >&2
    return 1
  }
}

docker network create "${NETWORK}" >/dev/null
docker run -d --rm \
  --name edge-ingress-api \
  --network "${NETWORK}" \
  --network-alias api \
  "${CADDY_IMAGE}" \
  caddy respond --listen :8000 --body api >/dev/null
docker run -d --rm \
  --name edge-ingress-frontend \
  --network "${NETWORK}" \
  --network-alias frontend \
  "${CADDY_IMAGE}" \
  caddy respond --listen :8080 --body frontend >/dev/null
docker run -d --rm \
  --name edge-ingress-caddy \
  --network "${NETWORK}" \
  -p "127.0.0.1:${PORT}:80" \
  -e "SCHOLIGHT_DOMAIN=${CANONICAL_DOMAIN}" \
  -e "SCHOLIGHT_EDGE_DOMAIN=${EDGE_DOMAIN}" \
  -e SCHOLIGHT_ACME_EMAIL=operator@example.invalid \
  -v "${PWD}/deploy/production/Caddyfile:/etc/caddy/Caddyfile:ro" \
  "${CADDY_IMAGE}" >/dev/null

for _ in {1..30}; do
  if curl --fail --silent --output /dev/null \
    --header 'Host: 127.0.0.1' "http://127.0.0.1:${PORT}/healthz" 2>/dev/null; then
    break
  fi
  sleep 0.2
done

assert_status 200 127.0.0.1 /healthz
assert_status 404 unknown.example.invalid /
assert_status 404 "${EDGE_DOMAIN}" /api/livez
assert_status 308 "${CANONICAL_DOMAIN}" /

[[ $(curl --fail --silent --header "Host: ${EDGE_DOMAIN}" \
  "http://127.0.0.1:${PORT}/") == frontend ]]
[[ $(curl --fail --silent --header "Host: ${EDGE_DOMAIN}" \
  "http://127.0.0.1:${PORT}/api/search") == api ]]
