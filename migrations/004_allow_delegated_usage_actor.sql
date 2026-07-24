-- scholight: migration-phase=contract
-- This atomic replacement only broadens the accepted actor values. It does not remove data.
ALTER TABLE scholight.usage_events
    DROP CONSTRAINT usage_events_actor_type,
    ADD CONSTRAINT usage_events_actor_type
        CHECK (actor_type IN ('web', 'access_key', 'delegated')),
    DROP CONSTRAINT usage_events_key_actor,
    ADD CONSTRAINT usage_events_key_actor
        CHECK (
            (actor_type = 'access_key' AND access_key_id IS NOT NULL)
            OR (actor_type IN ('web', 'delegated') AND access_key_id IS NULL)
        );
