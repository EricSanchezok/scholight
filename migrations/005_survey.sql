-- scholight: migration-phase=contract
-- Survey has never shipped to production. This is its only initial schema;
-- there is deliberately no migration path from the abandoned prototype model.

-- Survey is a new quota strength, so its first release widens the existing
-- product-owned override constraint in the same atomic migration.
ALTER TABLE scholight.user_quota_overrides
    DROP CONSTRAINT user_quota_overrides_strength;

ALTER TABLE scholight.user_quota_overrides
    ADD CONSTRAINT user_quota_overrides_strength
    CHECK (strength IN ('standard', 'thorough', 'survey'));

CREATE TABLE scholight.survey_daily_usage (
    user_id          BIGINT NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    usage_date       DATE NOT NULL,
    reserved_count   INTEGER NOT NULL DEFAULT 0,
    succeeded_count  INTEGER NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, usage_date),
    CONSTRAINT survey_daily_usage_reserved_nonnegative CHECK (reserved_count >= 0),
    CONSTRAINT survey_daily_usage_succeeded_nonnegative CHECK (succeeded_count >= 0)
);

CREATE TABLE scholight.surveys (
    id                    UUID PRIMARY KEY,
    user_id               BIGINT NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    client_request_id     UUID NOT NULL,
    initial_request       TEXT NOT NULL,
    title                 TEXT,
    request_hash          CHAR(64),
    status                TEXT NOT NULL DEFAULT 'drafting',
    quota_date            DATE NOT NULL,
    quota_state           TEXT NOT NULL DEFAULT 'reserved',
    notify_on_completion  BOOLEAN NOT NULL DEFAULT FALSE,
    error_code            VARCHAR(64),
    error_message         TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at            TIMESTAMPTZ,
    finished_at           TIMESTAMPTZ,
    CONSTRAINT surveys_initial_request_not_blank CHECK (btrim(initial_request) <> ''),
    CONSTRAINT surveys_title_length CHECK (
        title IS NULL OR char_length(btrim(title)) BETWEEN 1 AND 160
    ),
    CONSTRAINT surveys_request_hash_format CHECK (
        request_hash IS NULL OR request_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT surveys_status CHECK (
        status IN ('drafting', 'queued', 'running', 'archiving', 'succeeded', 'failed', 'cancelled')
    ),
    CONSTRAINT surveys_quota_state CHECK (quota_state IN ('reserved', 'consumed', 'released')),
    CONSTRAINT surveys_terminal_time CHECK (
        (status IN ('succeeded', 'failed', 'cancelled') AND finished_at IS NOT NULL)
        OR (status NOT IN ('succeeded', 'failed', 'cancelled') AND finished_at IS NULL)
    ),
    CONSTRAINT surveys_quota_terminal CHECK (
        (status = 'succeeded' AND quota_state = 'consumed')
        OR (status = 'failed' AND quota_state IN ('consumed', 'released'))
        OR (status = 'cancelled' AND quota_state = 'released')
        OR (status = 'archiving' AND quota_state IN ('consumed', 'released'))
        OR (status IN ('drafting', 'queued', 'running') AND quota_state = 'reserved')
    ),
    UNIQUE (user_id, client_request_id)
);

CREATE INDEX surveys_user_created_idx
    ON scholight.surveys (user_id, created_at DESC, id DESC);

CREATE TABLE scholight.survey_drafts (
    id                 UUID PRIMARY KEY,
    survey_id          UUID NOT NULL REFERENCES scholight.surveys(id) ON DELETE CASCADE,
    client_request_id  UUID NOT NULL,
    revision           SMALLINT,
    source             TEXT NOT NULL,
    user_message       TEXT NOT NULL,
    markdown           TEXT,
    request_hash       CHAR(64),
    status             TEXT NOT NULL DEFAULT 'queued',
    based_on_revision  SMALLINT,
    error_code         VARCHAR(64),
    error_message      TEXT,
    lease_owner        UUID,
    lease_expires_at   TIMESTAMPTZ,
    heartbeat_at       TIMESTAMPTZ,
    queued_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_claim_at      TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at         TIMESTAMPTZ,
    finished_at        TIMESTAMPTZ,
    CONSTRAINT survey_drafts_source CHECK (source IN ('generated', 'manual')),
    CONSTRAINT survey_drafts_status CHECK (
        status IN ('queued', 'running', 'ready', 'failed', 'cancelled')
    ),
    CONSTRAINT survey_drafts_user_message_not_blank CHECK (btrim(user_message) <> ''),
    CONSTRAINT survey_drafts_revision_range CHECK (revision IS NULL OR revision BETWEEN 1 AND 10),
    CONSTRAINT survey_drafts_base_range CHECK (
        based_on_revision IS NULL OR based_on_revision BETWEEN 1 AND 10
    ),
    CONSTRAINT survey_drafts_request_hash_format CHECK (
        request_hash IS NULL OR request_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT survey_drafts_ready_content CHECK (
        (status = 'ready' AND revision IS NOT NULL AND markdown IS NOT NULL AND btrim(markdown) <> '')
        OR (status <> 'ready' AND revision IS NULL AND markdown IS NULL)
    ),
    CONSTRAINT survey_drafts_lease_state CHECK (
        (lease_owner IS NULL AND lease_expires_at IS NULL)
        OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    UNIQUE (survey_id, client_request_id),
    UNIQUE (survey_id, revision),
    UNIQUE (survey_id, id)
);

CREATE UNIQUE INDEX survey_drafts_one_active_idx
    ON scholight.survey_drafts (survey_id)
    WHERE status IN ('queued', 'running');

CREATE INDEX survey_drafts_claim_idx
    ON scholight.survey_drafts (created_at, id)
    WHERE status = 'queued';

CREATE INDEX survey_drafts_lease_idx
    ON scholight.survey_drafts (lease_expires_at)
    WHERE status = 'running';

CREATE INDEX survey_drafts_fair_claim_idx
    ON scholight.survey_drafts (queued_at, survey_id, id)
    WHERE status = 'queued';

CREATE TABLE scholight.survey_jobs (
    id                   UUID PRIMARY KEY,
    survey_id            UUID NOT NULL UNIQUE REFERENCES scholight.surveys(id) ON DELETE CASCADE,
    approved_draft_id    UUID NOT NULL,
    client_request_id    UUID NOT NULL,
    request_hash         CHAR(64),
    status               TEXT NOT NULL DEFAULT 'queued',
    terminal_outcome     TEXT,
    storage_bucket       TEXT,
    storage_prefix       TEXT,
    manifest_key         TEXT,
    error_code           VARCHAR(64),
    error_message        TEXT,
    lease_owner          UUID,
    lease_expires_at     TIMESTAMPTZ,
    heartbeat_at         TIMESTAMPTZ,
    cancel_requested_at  TIMESTAMPTZ,
    archive_attempts     INTEGER NOT NULL DEFAULT 0,
    next_archive_at      TIMESTAMPTZ,
    progress_stage       TEXT NOT NULL DEFAULT 'waiting',
    progress_updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    queued_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_claim_at        TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at           TIMESTAMPTZ,
    finished_at          TIMESTAMPTZ,
    CONSTRAINT survey_jobs_status CHECK (status IN ('queued', 'running', 'archiving', 'finished')),
    CONSTRAINT survey_jobs_terminal_outcome CHECK (
        terminal_outcome IS NULL OR terminal_outcome IN ('succeeded', 'failed', 'cancelled')
    ),
    CONSTRAINT survey_jobs_terminal_state CHECK (
        (status IN ('queued', 'running') AND terminal_outcome IS NULL)
        OR (status IN ('archiving', 'finished') AND terminal_outcome IS NOT NULL)
    ),
    CONSTRAINT survey_jobs_manifest_state CHECK (
        (status = 'finished' AND storage_prefix IS NOT NULL AND manifest_key IS NOT NULL
            AND finished_at IS NOT NULL)
        OR status <> 'finished'
    ),
    CONSTRAINT survey_jobs_storage_state CHECK (
        (status = 'finished' AND storage_bucket IS NOT NULL)
        OR status <> 'finished'
    ),
    CONSTRAINT survey_jobs_lease_state CHECK (
        (lease_owner IS NULL AND lease_expires_at IS NULL)
        OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CONSTRAINT survey_jobs_request_hash_format CHECK (
        request_hash IS NULL OR request_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT survey_jobs_progress_stage CHECK (
        progress_stage IN (
            'waiting', 'planning', 'discovering', 'reviewing_evidence',
            'structuring_report', 'writing_report', 'finalizing'
        )
    ),
    CONSTRAINT survey_jobs_archive_attempts_nonnegative CHECK (archive_attempts >= 0),
    CONSTRAINT survey_jobs_approved_draft_owner FOREIGN KEY (survey_id, approved_draft_id)
        REFERENCES scholight.survey_drafts(survey_id, id) ON DELETE RESTRICT,
    UNIQUE (survey_id, client_request_id)
);

CREATE INDEX survey_jobs_queued_claim_idx
    ON scholight.survey_jobs (created_at, id)
    WHERE status = 'queued';

CREATE INDEX survey_jobs_archive_claim_idx
    ON scholight.survey_jobs (next_archive_at, created_at, id)
    WHERE status = 'archiving';

CREATE INDEX survey_jobs_running_lease_idx
    ON scholight.survey_jobs (lease_expires_at)
    WHERE status = 'running';

CREATE INDEX survey_jobs_fair_claim_idx
    ON scholight.survey_jobs (queued_at, survey_id, id)
    WHERE status = 'queued';

CREATE INDEX survey_jobs_cancel_requested_idx
    ON scholight.survey_jobs (cancel_requested_at, id)
    WHERE status = 'running' AND cancel_requested_at IS NOT NULL;

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
