-- Recovery ledger only: these records are not runnable ingestion jobs.
CREATE TABLE scholight.deferred_fulltext (
    arxiv_id VARCHAR(32) PRIMARY KEY,
    target_version INTEGER NOT NULL CHECK (target_version > 0),
    first_seen_date DATE NOT NULL,
    last_seen_date DATE NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
