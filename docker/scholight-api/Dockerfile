# Scholight — reproducible shared API and scheduler image.
# sanchezcloud-identity is fetched from an immutable reviewed Git revision; local ignored
# working trees never enter the production image.

ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba
FROM ${PYTHON_IMAGE} AS builder

ARG SANCHEZCLOUD_IDENTITY_ARCHIVE=https://api.github.com/repos/EricSanchezok/sanchezcloud-identity/tarball
ARG SANCHEZCLOUD_IDENTITY_REVISION=6acd693b52257a229cfb84b5ad252fb6d7a5d263
ARG RCM_VERSION=v0.2.9
ARG RCM_LINUX_X86_64_SHA256=6e9009876b0d862d14618e0a8b7b92b51ed58f99229d12d74b61e64226cbd9a8
ARG TARGETARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir uv==0.9.2

WORKDIR /app
RUN --mount=type=secret,id=github_token \
    mkdir -p /app/sanchezcloud-identity \
    && curl --fail --location --silent --show-error \
        --header "Authorization: Bearer $(tr -d '\n' </run/secrets/github_token)" \
        --header "X-GitHub-Api-Version: 2022-11-28" \
        --output /tmp/sanchezcloud-identity.tar.gz \
        "${SANCHEZCLOUD_IDENTITY_ARCHIVE}/${SANCHEZCLOUD_IDENTITY_REVISION}" \
    && tar -xzf /tmp/sanchezcloud-identity.tar.gz --strip-components=1 -C /app/sanchezcloud-identity \
    && rm /tmp/sanchezcloud-identity.tar.gz

RUN test "${TARGETARCH}" = amd64 \
    && mkdir -p /app/bin /tmp/rcm \
    && curl --fail --location --silent --show-error \
        --output /tmp/accelerate.tar.gz \
        "https://github.com/EricSanchezok/rcm-dist/releases/download/${RCM_VERSION}/accelerate-x86_64-linux.tar.gz" \
    && printf '%s  %s\n' \
        "${RCM_LINUX_X86_64_SHA256}" \
        /tmp/accelerate.tar.gz \
        | sha256sum --check - \
    && tar -xzf /tmp/accelerate.tar.gz -C /tmp/rcm \
    && install -m 0755 /tmp/rcm/accelerate /app/bin/accelerate \
    && rm -rf /tmp/rcm /tmp/accelerate.tar.gz

COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-dev --no-install-project --no-install-package sanchezcloud-identity \
    && uv pip install /app/sanchezcloud-identity/

COPY scholight/ /app/scholight/
COPY migrations/ /app/migrations/
COPY docker/scholight-api/start.py /app/start.py
RUN uv pip install --no-deps /app

FROM ${PYTHON_IMAGE} AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl pandoc poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 scholight \
    && mkdir -p /app /data \
    && chown scholight:scholight /data

WORKDIR /app
COPY --from=builder /app/.venv/ /app/.venv/
COPY --from=builder /app/scholight/ /app/scholight/
COPY --from=builder /app/migrations/ /app/migrations/
COPY --from=builder /app/start.py /app/start.py
COPY --from=builder /app/bin/accelerate /usr/local/bin/accelerate
COPY global-bundle.pem /etc/ssl/certs/global-bundle.pem
COPY deploy/production/bootstrap.sh /opt/scholight-package/bootstrap.sh
COPY deploy/production/release.sh /opt/scholight-package/release.sh
COPY deploy/production/compose-command.sh /opt/scholight-package/compose-command.sh
COPY deploy/production/smoke.sh /opt/scholight-package/smoke.sh
COPY deploy/production/wait-ssm.sh /opt/scholight-package/wait-ssm.sh
COPY deploy/production/compose.yaml /opt/scholight-package/compose.yaml
COPY deploy/production/Caddyfile /opt/scholight-package/Caddyfile
COPY deploy/production/cloudwatch-agent.json /opt/scholight-package/cloudwatch-agent.json
COPY deploy/production/bootstrap-db.sql /opt/scholight-package/bootstrap-db.sql
RUN chmod 0755 \
      /opt/scholight-package/bootstrap.sh \
      /opt/scholight-package/release.sh \
      /opt/scholight-package/compose-command.sh \
      /opt/scholight-package/smoke.sh \
      /opt/scholight-package/wait-ssm.sh \
    && chmod 0600 /opt/scholight-package/bootstrap-db.sql \
    && chmod 0644 \
      /opt/scholight-package/compose.yaml \
      /opt/scholight-package/Caddyfile \
      /opt/scholight-package/cloudwatch-agent.json

USER scholight
EXPOSE 8000

ENV SCHOLIGHT_DATA_ROOT=/data
ENV SCHOLIGHT_PG_SSL_ROOT_CERT=/etc/ssl/certs/global-bundle.pem
ENV SCHOLIGHT_SERVER_HOST=0.0.0.0
ENV SCHOLIGHT_SERVER_PORT=8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/livez || exit 1

CMD ["/app/.venv/bin/python", "/app/start.py"]
