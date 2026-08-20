-- scholight: migration-phase=contract
-- This atomic replacement only broadens succeeded Survey quota settlement so a readable,
-- quality-degraded report can be delivered without consuming the user's daily allowance.
-- Existing states remain valid and no data is rewritten.
ALTER TABLE scholight.surveys
    DROP CONSTRAINT surveys_quota_terminal,
    ADD CONSTRAINT surveys_quota_terminal CHECK (
        (status = 'succeeded' AND quota_state IN ('consumed', 'released'))
        OR (status = 'failed' AND quota_state IN ('consumed', 'released'))
        OR (status = 'cancelled' AND quota_state = 'released')
        OR (status = 'archiving' AND quota_state IN ('consumed', 'released'))
        OR (status IN ('drafting', 'queued', 'running') AND quota_state = 'reserved')
    );
