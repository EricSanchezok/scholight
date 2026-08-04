ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba
ARG BASE_IMAGE=scholight-survey:ci
FROM ${PYTHON_IMAGE} AS e2e-python-builder

ARG SANCHEZCLOUD_IDENTITY_ARCHIVE=https://api.github.com/repos/EricSanchezok/sanchezcloud-identity/tarball
ARG SANCHEZCLOUD_IDENTITY_REVISION=6acd693b52257a229cfb84b5ad252fb6d7a5d263

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir uv==0.9.2

WORKDIR /app
COPY pyproject.toml uv.lock /app/
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
RUN uv sync --frozen --no-dev --extra api --extra extract --extra survey \
        --no-install-project \
        --no-install-package sanchezcloud-identity \
    && uv pip install --no-deps /app/sanchezcloud-identity/
COPY scholight /app/scholight
COPY migrations /app/migrations
RUN uv pip install --no-deps /app

FROM ${BASE_IMAGE}

USER root
COPY --from=e2e-python-builder /app/.venv/ /app/.venv/
COPY --chown=scholight:scholight scholight /app/scholight
COPY --chown=scholight:scholight migrations /app/migrations
COPY --chown=scholight:scholight tests/survey_e2e /app/survey_e2e
# Console-script entry points otherwise resolve the package installed in the
# base image before this E2E overlay. Force every service to exercise this tree.
ENV PYTHONPATH=/app
# Exercise the vendored production graph and prompts unchanged. Only redirect the
# model network boundary inside this hermetic image; production files in the repo
# remain byte-for-byte untouched.
RUN set -eu; \
    workflow_root=$(/app/.venv/bin/python -c \
      'from pathlib import Path; import scholight.survey; print(Path(scholight.survey.__file__).parent / "workflow")'); \
    for root in /app/scholight/survey/workflow "${workflow_root}"; do \
      find "${root}/rcm" -type f -name '*.rcm' -exec \
        sed -i 's#endpoint = "https://api.deepseek.com"#endpoint = "http://model:8080/v1"#g' {} +; \
    done
USER scholight
