# Scholight Web Extract sidecar: Chromium and document parsing stay outside the search API.

ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba
FROM ${PYTHON_IMAGE} AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir uv==0.9.2

WORKDIR /app
COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-dev --extra extract --no-install-project \
    --no-install-package sanchezcloud-identity

COPY scholight/ /app/scholight/
RUN uv pip install --no-deps /app

FROM ${PYTHON_IMAGE} AS runtime

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY --from=builder /app/.venv/ /app/.venv/
RUN /app/.venv/bin/playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && useradd --create-home --uid 1000 scholight \
    && mkdir -p /app /data \
    && chown scholight:scholight /data

WORKDIR /app
COPY --from=builder /app/scholight/ /app/scholight/
COPY docker/scholight-extract/start.py /app/start.py

USER scholight
EXPOSE 8001

ENV SCHOLIGHT_DATA_ROOT=/data
ENV SCHOLIGHT_EXTRACT_SERVER_HOST=0.0.0.0
ENV SCHOLIGHT_EXTRACT_SERVER_PORT=8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/livez', timeout=3)"]

CMD ["/app/.venv/bin/python", "/app/start.py"]
