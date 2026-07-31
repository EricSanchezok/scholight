DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM scholight.survey_jobs LIMIT 1) THEN
        RAISE EXCEPTION
            'survey aggregate migration requires the disabled legacy survey_jobs table to be empty';
    END IF;
END
$$;

DROP TABLE scholight.survey_jobs;

CREATE TABLE scholight.surveys (
    id                 UUID PRIMARY KEY,
    user_id            BIGINT NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    client_request_id  UUID NOT NULL,
    initial_request    TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'drafting',
    quota_date         DATE NOT NULL,
    quota_state        TEXT NOT NULL DEFAULT 'reserved',
    error_code         VARCHAR(64),
    error_message      TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at         TIMESTAMPTZ,
    finished_at        TIMESTAMPTZ,
    CONSTRAINT surveys_initial_request_not_blank CHECK (btrim(initial_request) <> ''),
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
    status             TEXT NOT NULL DEFAULT 'queued',
    based_on_revision  SMALLINT,
    error_code         VARCHAR(64),
    error_message      TEXT,
    lease_owner        UUID,
    lease_expires_at   TIMESTAMPTZ,
    heartbeat_at       TIMESTAMPTZ,
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

CREATE TABLE scholight.survey_jobs (
    id                 UUID PRIMARY KEY,
    survey_id          UUID NOT NULL UNIQUE REFERENCES scholight.surveys(id) ON DELETE CASCADE,
    approved_draft_id  UUID NOT NULL,
    client_request_id  UUID NOT NULL,
    status             TEXT NOT NULL DEFAULT 'queued',
    terminal_outcome   TEXT,
    storage_prefix     TEXT,
    manifest_key       TEXT,
    error_code         VARCHAR(64),
    error_message      TEXT,
    lease_owner        UUID,
    lease_expires_at   TIMESTAMPTZ,
    heartbeat_at       TIMESTAMPTZ,
    progress_stage     TEXT NOT NULL DEFAULT 'waiting',
    progress_updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    archive_attempts   INTEGER NOT NULL DEFAULT 0,
    next_archive_at    TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at         TIMESTAMPTZ,
    finished_at        TIMESTAMPTZ,
    CONSTRAINT survey_jobs_status CHECK (status IN ('queued', 'running', 'archiving', 'finished')),
    CONSTRAINT survey_jobs_terminal_outcome CHECK (
        terminal_outcome IS NULL OR terminal_outcome IN ('succeeded', 'failed')
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
    CONSTRAINT survey_jobs_lease_state CHECK (
        (lease_owner IS NULL AND lease_expires_at IS NULL)
        OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
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
