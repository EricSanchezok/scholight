# Scholight Python images use separate dependency profiles so each runtime contains
# only the libraries and system tools needed by that component.

ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba
FROM ${PYTHON_IMAGE} AS builder-base

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir uv==0.9.2

WORKDIR /app
COPY pyproject.toml uv.lock /app/


FROM builder-base AS api-builder

ARG SANCHEZCLOUD_IDENTITY_ARCHIVE=https://api.github.com/repos/EricSanchezok/sanchezcloud-identity/tarball
ARG SANCHEZCLOUD_IDENTITY_REVISION=6acd693b52257a229cfb84b5ad252fb6d7a5d263

RUN --mount=type=secret,id=github_token \
    mkdir -p /app/sanchezcloud-identity \
    && curl --fail --location --silent --show-error \
        --header "Authorization: Bearer $(tr -d '\n' </run/secrets/github_token)" \
        --header "X-GitHub-Api-Version: 2022-11-28" \
        --output /tmp/sanchezcloud-identity.tar.gz \
        "${SANCHEZCLOUD_IDENTITY_ARCHIVE}/${SANCHEZCLOUD_IDENTITY_REVISION}" \
    && tar -xzf /tmp/sanchezcloud-identity.tar.gz \
        --strip-components=1 -C /app/sanchezcloud-identity \
    && rm /tmp/sanchezcloud-identity.tar.gz
RUN uv sync --frozen --no-dev --extra api --no-install-project \
        --no-install-package sanchezcloud-identity \
    && uv pip install /app/sanchezcloud-identity/
COPY scholight/ /app/scholight/
COPY migrations/ /app/migrations/
RUN uv pip install --no-deps /app


FROM builder-base AS ingest-builder

RUN uv sync --frozen --no-dev --extra ingest --no-install-project
COPY scholight/ /app/scholight/
COPY migrations/ /app/migrations/
RUN uv pip install --no-deps /app


FROM builder-base AS survey-python-builder

RUN uv sync --frozen --no-dev --extra survey --no-install-project
COPY scholight/ /app/scholight/
COPY migrations/ /app/migrations/
RUN uv pip install --no-deps /app


FROM survey-python-builder AS survey-builder

ARG RCM_VERSION=v0.2.19
ARG RCM_LINUX_X86_64_SHA256=afe5c1c6fb7ad6842543faffa418c19ce8b145a2c955b93cd65e5ecd80a688f1
ARG TARGETARCH

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


FROM ${PYTHON_IMAGE} AS runtime-base

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 scholight \
    && mkdir -p /app /data \
    && chown scholight:scholight /data

WORKDIR /app
COPY global-bundle.pem /etc/ssl/certs/global-bundle.pem

ENV PATH=/app/.venv/bin:${PATH}
ENV SCHOLIGHT_DATA_ROOT=/data
ENV SCHOLIGHT_PG_SSL_ROOT_CERT=/etc/ssl/certs/global-bundle.pem

USER scholight
STOPSIGNAL SIGTERM


FROM runtime-base AS api

COPY --from=api-builder /app/.venv/ /app/.venv/
COPY --from=api-builder /app/migrations/ /app/migrations/
COPY global-bundle.pem /etc/ssl/certs/global-bundle.pem
COPY docker/scholight-api/start.py /app/start.py

ENV PATH=/app/.venv/bin:${PATH}
ENV SCHOLIGHT_DATA_ROOT=/data
ENV SCHOLIGHT_PG_SSL_ROOT_CERT=/etc/ssl/certs/global-bundle.pem
ENV SCHOLIGHT_SERVER_HOST=0.0.0.0
ENV SCHOLIGHT_SERVER_PORT=8000

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/livez', timeout=3)"]

CMD ["python", "/app/start.py"]


FROM runtime-base AS ingest

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends pandoc poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ingest-builder /app/.venv/ /app/.venv/
COPY --from=ingest-builder /app/migrations/ /app/migrations/
COPY global-bundle.pem /etc/ssl/certs/global-bundle.pem

ENV PATH=/app/.venv/bin:${PATH}
ENV SCHOLIGHT_DATA_ROOT=/data
ENV SCHOLIGHT_PG_SSL_ROOT_CERT=/etc/ssl/certs/global-bundle.pem

USER scholight
STOPSIGNAL SIGTERM
CMD ["scholight", "scheduler", "drain-ingest"]


FROM runtime-base AS survey

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY --from=survey-builder /app/.venv/ /app/.venv/
COPY --from=survey-builder /app/migrations/ /app/migrations/
COPY --from=survey-builder /app/bin/accelerate /usr/local/bin/accelerate
COPY global-bundle.pem /etc/ssl/certs/global-bundle.pem

ENV PATH=/app/.venv/bin:${PATH}
ENV SCHOLIGHT_DATA_ROOT=/data
ENV SCHOLIGHT_PG_SSL_ROOT_CERT=/etc/ssl/certs/global-bundle.pem

USER scholight
CMD ["scholight", "survey", "serve-worker"]
