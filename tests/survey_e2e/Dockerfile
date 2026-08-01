ARG BASE_IMAGE=scholight-api:ci
FROM ${BASE_IMAGE}

USER root
COPY --chown=scholight:scholight tests/survey_e2e /app/survey_e2e
COPY --chown=scholight:scholight tests/survey_e2e/workflows/draft.rcm \
    /app/scholight/survey/workflow/rcm/draft.rcm
COPY --chown=scholight:scholight tests/survey_e2e/workflows/survey_pipeline.rcm \
    /app/scholight/survey/workflow/rcm/survey_pipeline.rcm
COPY --chown=scholight:scholight tests/survey_e2e/workflows/draft.txt \
    /app/scholight/survey/workflow/prompts/e2e_draft.txt
COPY --chown=scholight:scholight tests/survey_e2e/workflows/survey.txt \
    /app/scholight/survey/workflow/prompts/e2e_survey.txt
COPY --chown=scholight:scholight tests/survey_e2e/workflows/draft.rcm \
    /app/.venv/lib/python3.11/site-packages/scholight/survey/workflow/rcm/draft.rcm
COPY --chown=scholight:scholight tests/survey_e2e/workflows/survey_pipeline.rcm \
    /app/.venv/lib/python3.11/site-packages/scholight/survey/workflow/rcm/survey_pipeline.rcm
COPY --chown=scholight:scholight tests/survey_e2e/workflows/draft.txt \
    /app/.venv/lib/python3.11/site-packages/scholight/survey/workflow/prompts/e2e_draft.txt
COPY --chown=scholight:scholight tests/survey_e2e/workflows/survey.txt \
    /app/.venv/lib/python3.11/site-packages/scholight/survey/workflow/prompts/e2e_survey.txt
USER scholight
