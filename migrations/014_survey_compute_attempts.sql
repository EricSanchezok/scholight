-- Durable, job-scoped Survey compute attempts and resumable workspace checkpoints.
-- This is an expand-only migration. Legacy Draft and Full workers ignore these objects.

ALTER TABLE scholight.survey_jobs
    ADD COLUMN workflow_version VARCHAR(64),
    ADD COLUMN executor_version VARCHAR(64),
    ADD COLUMN execution_deadline_at TIMESTAMPTZ,
    ADD COLUMN checkpoint_sequence INTEGER,
    ADD COLUMN checkpoint_stage VARCHAR(64),
    ADD COLUMN checkpoint_manifest_key TEXT,
    ADD COLUMN checkpoint_manifest_sha256 CHAR(64),
    ADD COLUMN resume_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE scholight.survey_jobs
    ADD CONSTRAINT survey_jobs_checkpoint_sequence_nonnegative
        CHECK (checkpoint_sequence IS NULL OR checkpoint_sequence >= 0),
    ADD CONSTRAINT survey_jobs_checkpoint_sha256_format
        CHECK (
            checkpoint_manifest_sha256 IS NULL
            OR checkpoint_manifest_sha256 ~ '^[0-9a-f]{64}$'
        ),
    ADD CONSTRAINT survey_jobs_checkpoint_pointer_state
        CHECK (
            (checkpoint_sequence IS NULL
                AND checkpoint_stage IS NULL
                AND checkpoint_manifest_key IS NULL
                AND checkpoint_manifest_sha256 IS NULL)
            OR (checkpoint_sequence IS NOT NULL
                AND checkpoint_stage IS NOT NULL
                AND checkpoint_manifest_key IS NOT NULL
                AND checkpoint_manifest_sha256 IS NOT NULL)
        ),
    ADD CONSTRAINT survey_jobs_resume_count_nonnegative CHECK (resume_count >= 0);

CREATE TABLE scholight.survey_compute_attempts (
    id                         UUID PRIMARY KEY,
    work_kind                  TEXT NOT NULL,
    survey_id                  UUID NOT NULL REFERENCES scholight.surveys(id) ON DELETE CASCADE,
    draft_id                   UUID REFERENCES scholight.survey_drafts(id) ON DELETE CASCADE,
    job_id                     UUID REFERENCES scholight.survey_jobs(id) ON DELETE CASCADE,
    attempt_no                 INTEGER NOT NULL,
    status                     TEXT NOT NULL DEFAULT 'reserved',
    resource_profile           TEXT NOT NULL,
    task_definition_arn        TEXT NOT NULL,
    client_token               VARCHAR(64) NOT NULL,
    ecs_task_arn               TEXT,
    ecs_event_version          BIGINT,
    current_stage              VARCHAR(64),
    current_unit               VARCHAR(160),
    checkpoint_sequence        INTEGER,
    launch_failures            INTEGER NOT NULL DEFAULT 0,
    next_launch_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    exit_code                  INTEGER,
    stop_code                  VARCHAR(128),
    stopped_reason             TEXT,
    failure_class              VARCHAR(64),
    failure_details            JSONB NOT NULL DEFAULT '{}'::jsonb,
    peak_memory_bytes          BIGINT,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    launched_at                TIMESTAMPTZ,
    started_at                 TIMESTAMPTZ,
    heartbeat_at               TIMESTAMPTZ,
    stopped_at                 TIMESTAMPTZ,
    CONSTRAINT survey_compute_attempts_work_kind
        CHECK (work_kind IN ('draft', 'full')),
    CONSTRAINT survey_compute_attempts_target
        CHECK (
            (work_kind = 'draft' AND draft_id IS NOT NULL AND job_id IS NULL)
            OR (work_kind = 'full' AND draft_id IS NULL AND job_id IS NOT NULL)
        ),
    CONSTRAINT survey_compute_attempts_status
        CHECK (
            status IN (
                'reserved', 'launching', 'launched', 'running',
                'succeeded', 'retryable', 'failed', 'cancelled'
            )
        ),
    CONSTRAINT survey_compute_attempts_resource_profile
        CHECK (resource_profile IN ('draft', 'full-standard', 'full-high-memory')),
    CONSTRAINT survey_compute_attempts_attempt_no_positive CHECK (attempt_no > 0),
    CONSTRAINT survey_compute_attempts_client_token_nonempty CHECK (length(client_token) > 0),
    CONSTRAINT survey_compute_attempts_checkpoint_sequence_nonnegative
        CHECK (checkpoint_sequence IS NULL OR checkpoint_sequence >= 0),
    CONSTRAINT survey_compute_attempts_launch_failures_nonnegative CHECK (launch_failures >= 0),
    CONSTRAINT survey_compute_attempts_failure_details_object
        CHECK (jsonb_typeof(failure_details) = 'object'),
    CONSTRAINT survey_compute_attempts_peak_memory_nonnegative
        CHECK (peak_memory_bytes IS NULL OR peak_memory_bytes >= 0),
    UNIQUE (draft_id, attempt_no),
    UNIQUE (job_id, attempt_no),
    UNIQUE (client_token)
);

CREATE UNIQUE INDEX survey_compute_attempts_active_draft_idx
    ON scholight.survey_compute_attempts (draft_id)
    WHERE draft_id IS NOT NULL
      AND status IN ('reserved', 'launching', 'launched', 'running');

CREATE UNIQUE INDEX survey_compute_attempts_active_job_idx
    ON scholight.survey_compute_attempts (job_id)
    WHERE job_id IS NOT NULL
      AND status IN ('reserved', 'launching', 'launched', 'running');

CREATE UNIQUE INDEX survey_compute_attempts_task_arn_idx
    ON scholight.survey_compute_attempts (ecs_task_arn)
    WHERE ecs_task_arn IS NOT NULL;

CREATE INDEX survey_compute_attempts_launch_idx
    ON scholight.survey_compute_attempts (next_launch_at, created_at, id)
    WHERE status IN ('reserved', 'launching');

CREATE INDEX survey_compute_attempts_reconcile_idx
    ON scholight.survey_compute_attempts (heartbeat_at, launched_at, created_at, id)
    WHERE status IN ('launching', 'launched', 'running');
