-- Migration: 005_create_search_history
-- Description: Search query history with filters and timing metadata
-- Depends on: cloud-auth 002_create_users

BEGIN;

CREATE TABLE IF NOT EXISTS search_history (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    query_text      TEXT NOT NULL,
    level           SMALLINT NOT NULL CHECK (level IN (1, 2, 3)),
    strategy        VARCHAR(64),
    filters         JSONB,
    num_results     INTEGER NOT NULL DEFAULT 0 CHECK (num_results >= 0),
    response_time_ms REAL NOT NULL DEFAULT 0 CHECK (response_time_ms >= 0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ
);

-- User-facing: "show me my recent searches"
CREATE INDEX IF NOT EXISTS idx_search_history_user_time
    ON search_history (user_id, created_at DESC)
    WHERE deleted_at IS NULL;

-- Analytics: aggregate by creation date
CREATE INDEX IF NOT EXISTS idx_search_history_created
    ON search_history (created_at);

COMMIT;
