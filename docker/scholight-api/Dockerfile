# Scholight — reproducible shared API and scheduler image.
# cloud-auth is fetched from an immutable reviewed Git revision; local ignored
# working trees never enter the production image.

ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba
FROM ${PYTHON_IMAGE} AS builder

ARG PRIVATE_DEP_ARCHIVE=https://api.github.com/repos/EricSanchezok/cloud-auth/tarball
ARG PRIVATE_DEP_REVISION=a2831ef75f7d16d7fa7c8ab812917c6c49cb47b4

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir uv==0.9.2

WORKDIR /app
RUN --mount=type=secret,id=github_token \
    mkdir -p /app/cloud-auth \
    && curl --fail --location --silent --show-error \
        --header "Authorization: Bearer $(tr -d '\n' </run/secrets/github_token)" \
        --header "X-GitHub-Api-Version: 2022-11-28" \
        --output /tmp/cloud-auth.tar.gz \
        "${PRIVATE_DEP_ARCHIVE}/${PRIVATE_DEP_REVISION}" \
    && tar -xzf /tmp/cloud-auth.tar.gz --strip-components=1 -C /app/cloud-auth \
    && rm /tmp/cloud-auth.tar.gz

COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-dev --no-install-project --no-install-package cloud-auth \
    && uv pip install /app/cloud-auth/

COPY scholight/ /app/scholight/
COPY migrations/ /app/migrations/
COPY docker/scholight-api/start.py /app/start.py
RUN uv pip install --no-deps /app

FROM ${PYTHON_IMAGE} AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl pandoc \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 scholight \
    && mkdir -p /app /data \
    && chown scholight:scholight /data

WORKDIR /app
COPY --from=builder /app/.venv/ /app/.venv/
COPY --from=builder /app/cloud-auth/ /app/cloud-auth/
COPY --from=builder /app/scholight/ /app/scholight/
COPY --from=builder /app/migrations/ /app/migrations/
COPY --from=builder /app/start.py /app/start.py
COPY global-bundle.pem /etc/ssl/certs/global-bundle.pem

USER scholight
EXPOSE 8000

ENV SCHOLIGHT_DATA_ROOT=/data
ENV SCHOLIGHT_PG_SSL_ROOT_CERT=/etc/ssl/certs/global-bundle.pem
ENV SCHOLIGHT_SERVER_HOST=0.0.0.0
ENV SCHOLIGHT_SERVER_PORT=8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/livez || exit 1

CMD ["/app/.venv/bin/python", "/app/start.py"]
