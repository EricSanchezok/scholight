CREATE TABLE scholight.ingestion_sync_state (
    source TEXT PRIMARY KEY,
    last_successful_date DATE,
    last_started_at TIMESTAMPTZ,
    last_succeeded_at TIMESTAMPTZ,
    last_error_code TEXT,
    last_error_message TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE scholight.ingestion_jobs (
    arxiv_id VARCHAR(32) PRIMARY KEY,
    target_version INTEGER NOT NULL CHECK (target_version > 0),
    source TEXT NOT NULL CHECK (
        source IN ('new', 'revision', 'reconciliation', 'backfill', 'manual')
    ),
    priority SMALLINT NOT NULL DEFAULT 100,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'running', 'retry', 'succeeded', 'dead')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_error_code TEXT,
    last_error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    succeeded_at TIMESTAMPTZ,
    CHECK (
        (status = 'running' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR
        (status <> 'running' AND lease_owner IS NULL AND lease_expires_at IS NULL)
    )
);

CREATE INDEX ingestion_jobs_claim_idx
    ON scholight.ingestion_jobs (priority, available_at, created_at)
    WHERE status IN ('pending', 'retry');

CREATE INDEX ingestion_jobs_status_idx
    ON scholight.ingestion_jobs (status, updated_at DESC);

