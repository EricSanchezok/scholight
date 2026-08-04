ARG BASE_IMAGE=scholight-survey:ci
FROM ${BASE_IMAGE}

USER root
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
