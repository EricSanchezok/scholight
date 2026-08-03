ALTER TABLE scholight.surveys
    ADD COLUMN notify_on_completion BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE scholight.survey_email_notifications (
    id                  UUID PRIMARY KEY,
    survey_id           UUID NOT NULL REFERENCES scholight.surveys(id) ON DELETE CASCADE,
    user_id             BIGINT NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    survey_outcome      TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    attempts            INTEGER NOT NULL DEFAULT 0,
    next_attempt_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_owner         UUID,
    lease_expires_at    TIMESTAMPTZ,
    last_error          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    CONSTRAINT survey_email_notifications_outcome CHECK (
        survey_outcome IN ('succeeded', 'failed')
    ),
    CONSTRAINT survey_email_notifications_status CHECK (
        status IN ('pending', 'running', 'retry', 'succeeded', 'dead')
    ),
    CONSTRAINT survey_email_notifications_attempts_nonnegative CHECK (attempts >= 0),
    CONSTRAINT survey_email_notifications_lease_state CHECK (
        (lease_owner IS NULL AND lease_expires_at IS NULL)
        OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CONSTRAINT survey_email_notifications_finished_state CHECK (
        (status IN ('succeeded', 'dead') AND finished_at IS NOT NULL)
        OR (status NOT IN ('succeeded', 'dead') AND finished_at IS NULL)
    ),
    UNIQUE (survey_id)
);

CREATE INDEX survey_email_notifications_claim_idx
    ON scholight.survey_email_notifications (next_attempt_at, created_at, id)
    WHERE status IN ('pending', 'retry');

CREATE INDEX survey_email_notifications_lease_idx
    ON scholight.survey_email_notifications (lease_expires_at)
    WHERE status = 'running';
