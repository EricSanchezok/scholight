#!/bin/sh
set -eu
# Images are local build outputs; this script never publishes or deploys.
prefix="scholight-image-smoke-$$"
cleanup() {
  docker rm -f "$prefix-web" "$prefix-extract" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM
for component in api metadata; do
  docker run --rm -e SCHOLIGHT_DISABLE_DOTENV=1 \
    -v "$PWD/tests/images/lean_smoke.py:/app/lean_smoke.py:ro" \
    --entrypoint /app/.venv/bin/python "scholight-$component:lean-ci" \
    /app/lean_smoke.py "$component"
done
docker run --rm --entrypoint /app/.venv/bin/scholight \
  scholight-metadata:lean-ci scheduler sync --help >/dev/null
docker run -d --name "$prefix-web" --add-host api:127.0.0.1 \
  -e SCHOLIGHT_PUBLIC_WEB_URL=http://localhost:7200 scholight-web:lean-ci >/dev/null
docker run -d --name "$prefix-extract" -e SCHOLIGHT_DISABLE_DOTENV=1 \
  -e SCHOLIGHT_EXTRACT_INTERNAL_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
  scholight-extract:lean-ci >/dev/null
for component in web extract; do
  attempt=0
  until [ "$(docker inspect --format '{{.State.Health.Status}}' "$prefix-$component")" = healthy ]; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 60 ]; then
      docker logs "$prefix-$component"
      exit 1
    fi
    sleep 1
  done
done
docker exec "$prefix-web" wget -q -O /dev/null http://127.0.0.1:8080/
docker exec "$prefix-extract" /app/.venv/bin/python -c \
  'from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True, args=["--no-sandbox"]); page=b.new_page(); page.set_content("<p>Native browser works</p>"); assert page.inner_text("p")=="Native browser works"; b.close(); p.stop()'
