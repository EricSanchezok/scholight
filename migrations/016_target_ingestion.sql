-- Expand only. Legacy queues and cursors remain intact for N-1 application reads.
CREATE TABLE scholight.ingestion_targets (
    target_id TEXT PRIMARY KEY,
    identity JSONB NOT NULL,
    baseline_manifest TEXT,
    baseline_sha256 TEXT CHECK (baseline_sha256 IS NULL OR length(baseline_sha256) = 64),
    baseline_date DATE,
    claim_count BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((baseline_manifest IS NULL) = (baseline_date IS NULL))
);

CREATE TABLE scholight.target_ingestion_jobs (
    target_id TEXT NOT NULL REFERENCES scholight.ingestion_targets(target_id),
    arxiv_id VARCHAR(32) NOT NULL,
    target_version INTEGER NOT NULL CHECK (target_version > 0),
    profile_sha256 TEXT NOT NULL CHECK (length(profile_sha256) = 64),
    source TEXT NOT NULL CHECK (source IN ('new', 'revision', 'reconciliation', 'backfill', 'manual')),
    priority SMALLINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'retry', 'succeeded', 'dead')),
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
    PRIMARY KEY (target_id, arxiv_id),
    CHECK ((status = 'running' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status <> 'running' AND lease_owner IS NULL AND lease_expires_at IS NULL))
);
CREATE INDEX target_ingestion_claim_idx ON scholight.target_ingestion_jobs
    (target_id, priority, available_at, created_at) WHERE status IN ('pending', 'retry', 'running');

CREATE TABLE scholight.fulltext_scope (
    target_id TEXT NOT NULL REFERENCES scholight.ingestion_targets(target_id),
    arxiv_id VARCHAR(32) NOT NULL,
    target_version INTEGER NOT NULL CHECK (target_version > 0),
    reasons TEXT[] NOT NULL,
    first_seen_date DATE NOT NULL,
    last_seen_date DATE NOT NULL,
    PRIMARY KEY (target_id, arxiv_id)
);

CREATE TABLE scholight.fulltext_receipts (
    target_id TEXT NOT NULL REFERENCES scholight.ingestion_targets(target_id),
    arxiv_id VARCHAR(32) NOT NULL,
    paper_version INTEGER NOT NULL CHECK (paper_version > 0),
    profile_sha256 TEXT NOT NULL CHECK (length(profile_sha256) = 64),
    configuration JSONB NOT NULL,
    chunk_count INTEGER NOT NULL CHECK (chunk_count > 0),
    chunks_sha256 TEXT NOT NULL CHECK (length(chunks_sha256) = 64),
    recovery_manifest TEXT NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (target_id, arxiv_id, paper_version, profile_sha256)
);

CREATE TABLE scholight.fulltext_installs (
    target_id TEXT NOT NULL REFERENCES scholight.ingestion_targets(target_id),
    arxiv_id VARCHAR(32) NOT NULL,
    paper_version INTEGER NOT NULL CHECK (paper_version > 0),
    profile_sha256 TEXT NOT NULL CHECK (length(profile_sha256) = 64),
    recovery_manifest TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    stage TEXT NOT NULL CHECK (stage IN ('prepared', 'written', 'verified', 'cleaned', 'complete')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (target_id, arxiv_id, paper_version, profile_sha256)
);
