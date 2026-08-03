ALTER TABLE scholight.survey_jobs
    ADD COLUMN cancel_requested_at TIMESTAMPTZ;

ALTER TABLE scholight.survey_jobs
    DROP CONSTRAINT survey_jobs_terminal_outcome;

ALTER TABLE scholight.survey_jobs
    ADD CONSTRAINT survey_jobs_terminal_outcome CHECK (
        terminal_outcome IS NULL OR terminal_outcome IN ('succeeded', 'failed', 'cancelled')
    );

CREATE INDEX survey_jobs_cancel_requested_idx
    ON scholight.survey_jobs (cancel_requested_at, id)
    WHERE status = 'running' AND cancel_requested_at IS NOT NULL;
