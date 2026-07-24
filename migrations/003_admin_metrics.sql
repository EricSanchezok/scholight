ALTER TABLE scholight.usage_events
    ADD COLUMN transport TEXT NOT NULL DEFAULT 'rest',
    ADD CONSTRAINT usage_events_transport
        CHECK (transport IN ('rest', 'mcp'));

CREATE INDEX usage_events_created_idx
    ON scholight.usage_events (created_at);

CREATE INDEX ingestion_jobs_succeeded_idx
    ON scholight.ingestion_jobs (succeeded_at)
    WHERE succeeded_at IS NOT NULL;

CREATE INDEX user_profiles_created_idx
    ON scholight.user_profiles (created_at);

CREATE INDEX access_keys_created_idx
    ON scholight.access_keys (created_at);
