CREATE TABLE scholight.survey_daily_usage (
    user_id          BIGINT NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    usage_date       DATE NOT NULL,
    reserved_count   INTEGER NOT NULL DEFAULT 0,
    succeeded_count  INTEGER NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, usage_date),
    CONSTRAINT survey_daily_usage_reserved_nonnegative
        CHECK (reserved_count >= 0),
    CONSTRAINT survey_daily_usage_succeeded_nonnegative
        CHECK (succeeded_count >= 0)
);

CREATE TABLE scholight.survey_jobs (
    id                UUID PRIMARY KEY,
    user_id           BIGINT NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    topic             TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',
    terminal_outcome  TEXT,
    quota_date        DATE NOT NULL,
    storage_prefix    TEXT,
    manifest_key      TEXT,
    error_code        VARCHAR(64),
    error_message     TEXT,
    lease_owner       UUID,
    lease_expires_at  TIMESTAMPTZ,
    heartbeat_at      TIMESTAMPTZ,
    archive_attempts  INTEGER NOT NULL DEFAULT 0,
    next_archive_at   TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,
    CONSTRAINT survey_jobs_topic_not_blank
        CHECK (btrim(topic) <> ''),
    CONSTRAINT survey_jobs_status
        CHECK (status IN ('pending', 'running', 'archiving', 'succeeded', 'failed')),
    CONSTRAINT survey_jobs_terminal_outcome
        CHECK (terminal_outcome IS NULL OR terminal_outcome IN ('succeeded', 'failed')),
    CONSTRAINT survey_jobs_archive_attempts_nonnegative
        CHECK (archive_attempts >= 0),
    CONSTRAINT survey_jobs_terminal_state
        CHECK (
            (status IN ('pending', 'running') AND terminal_outcome IS NULL)
            OR (status IN ('archiving', 'succeeded', 'failed') AND terminal_outcome IS NOT NULL)
        ),
    CONSTRAINT survey_jobs_manifest_state
        CHECK (
            (status IN ('succeeded', 'failed') AND storage_prefix IS NOT NULL
                AND manifest_key IS NOT NULL AND finished_at IS NOT NULL)
            OR status IN ('pending', 'running', 'archiving')
        ),
    CONSTRAINT survey_jobs_lease_state
        CHECK (
            (lease_owner IS NULL AND lease_expires_at IS NULL)
            OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        )
);

CREATE INDEX survey_jobs_user_created_idx
    ON scholight.survey_jobs (user_id, created_at DESC);

CREATE INDEX survey_jobs_pending_claim_idx
    ON scholight.survey_jobs (created_at, id)
    WHERE status = 'pending';

CREATE INDEX survey_jobs_archive_claim_idx
    ON scholight.survey_jobs (next_archive_at, created_at, id)
    WHERE status = 'archiving';

CREATE INDEX survey_jobs_running_lease_idx
    ON scholight.survey_jobs (lease_expires_at)
    WHERE status = 'running';
