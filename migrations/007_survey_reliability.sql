ALTER TABLE scholight.surveys
    ADD COLUMN request_hash CHAR(64);

ALTER TABLE scholight.surveys
    ADD CONSTRAINT surveys_request_hash_format CHECK (
        request_hash IS NULL OR request_hash ~ '^[0-9a-f]{64}$'
    );

ALTER TABLE scholight.survey_drafts
    ADD COLUMN request_hash CHAR(64),
    ADD COLUMN queued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN last_claim_at TIMESTAMPTZ;

ALTER TABLE scholight.survey_drafts
    ADD CONSTRAINT survey_drafts_request_hash_format CHECK (
        request_hash IS NULL OR request_hash ~ '^[0-9a-f]{64}$'
    );

ALTER TABLE scholight.survey_jobs
    ADD COLUMN request_hash CHAR(64),
    ADD COLUMN storage_bucket TEXT,
    ADD COLUMN queued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN last_claim_at TIMESTAMPTZ,
    ADD COLUMN progress_stage TEXT NOT NULL DEFAULT 'waiting',
    ADD COLUMN progress_updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE scholight.survey_jobs
    ADD CONSTRAINT survey_jobs_request_hash_format CHECK (
        request_hash IS NULL OR request_hash ~ '^[0-9a-f]{64}$'
    ),
    ADD CONSTRAINT survey_jobs_progress_stage CHECK (
        progress_stage IN (
            'waiting', 'planning', 'discovering', 'reviewing_evidence',
            'structuring_report', 'writing_report', 'finalizing'
        )
    ),
    ADD CONSTRAINT survey_jobs_storage_state CHECK (
        (status = 'finished' AND storage_bucket IS NOT NULL)
        OR status <> 'finished'
    );

CREATE INDEX survey_drafts_fair_claim_idx
    ON scholight.survey_drafts (queued_at, survey_id, id)
    WHERE status = 'queued';

CREATE INDEX survey_jobs_fair_claim_idx
    ON scholight.survey_jobs (queued_at, survey_id, id)
    WHERE status = 'queued';

CREATE TABLE scholight.survey_artifact_cleanup_outbox (
    id                  UUID PRIMARY KEY,
    source_job_id       UUID NOT NULL UNIQUE,
    user_id_snapshot    BIGINT NOT NULL,
    bucket              TEXT NOT NULL,
    storage_prefix      TEXT NOT NULL,
    manifest_key        TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    attempts            INTEGER NOT NULL DEFAULT 0,
    next_attempt_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_owner         UUID,
    lease_expires_at    TIMESTAMPTZ,
    last_error          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    CONSTRAINT survey_artifact_cleanup_status CHECK (
        status IN ('pending', 'running', 'retry', 'succeeded', 'dead')
    ),
    CONSTRAINT survey_artifact_cleanup_attempts_nonnegative CHECK (attempts >= 0),
    CONSTRAINT survey_artifact_cleanup_lease_state CHECK (
        (lease_owner IS NULL AND lease_expires_at IS NULL)
        OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CONSTRAINT survey_artifact_cleanup_finished_state CHECK (
        (status IN ('succeeded', 'dead') AND finished_at IS NOT NULL)
        OR (status NOT IN ('succeeded', 'dead') AND finished_at IS NULL)
    )
);

CREATE INDEX survey_artifact_cleanup_claim_idx
    ON scholight.survey_artifact_cleanup_outbox (next_attempt_at, created_at, id)
    WHERE status IN ('pending', 'retry');

CREATE INDEX survey_artifact_cleanup_lease_idx
    ON scholight.survey_artifact_cleanup_outbox (lease_expires_at)
    WHERE status = 'running';

CREATE FUNCTION scholight.enqueue_survey_artifact_cleanup()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, scholight
AS $$
BEGIN
    INSERT INTO scholight.survey_artifact_cleanup_outbox (
        id,
        source_job_id,
        user_id_snapshot,
        bucket,
        storage_prefix,
        manifest_key
    )
    SELECT
        gen_random_uuid(),
        jobs.id,
        OLD.user_id,
        jobs.storage_bucket,
        jobs.storage_prefix,
        jobs.manifest_key
    FROM scholight.survey_jobs AS jobs
    WHERE jobs.survey_id = OLD.id
      AND jobs.storage_bucket IS NOT NULL
      AND jobs.storage_prefix IS NOT NULL
      AND jobs.manifest_key IS NOT NULL
    ON CONFLICT (source_job_id) DO NOTHING;
    RETURN OLD;
END;
$$;

CREATE TRIGGER surveys_enqueue_artifact_cleanup
BEFORE DELETE ON scholight.surveys
FOR EACH ROW
EXECUTE FUNCTION scholight.enqueue_survey_artifact_cleanup();
