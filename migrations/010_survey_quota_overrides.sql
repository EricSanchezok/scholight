-- scholight: migration-phase=contract
-- Expand the existing per-user quota override keyspace to include Survey.

ALTER TABLE scholight.user_quota_overrides
    DROP CONSTRAINT user_quota_overrides_strength;

ALTER TABLE scholight.user_quota_overrides
    ADD CONSTRAINT user_quota_overrides_strength
    CHECK (strength IN ('standard', 'thorough', 'survey'));
