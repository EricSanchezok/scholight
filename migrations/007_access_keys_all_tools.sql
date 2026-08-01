-- scholight: migration-phase=contract
-- Personal Access Keys are a product credential, not a per-tool permission system.
ALTER TABLE scholight.access_keys
    DROP CONSTRAINT access_keys_search_scope_only;

UPDATE scholight.access_keys
SET scopes = ARRAY['all']::TEXT[];

ALTER TABLE scholight.access_keys
    ALTER COLUMN scopes SET DEFAULT ARRAY['all']::TEXT[],
    ADD CONSTRAINT access_keys_all_tools_scope
        CHECK (scopes = ARRAY['all']::TEXT[]);
