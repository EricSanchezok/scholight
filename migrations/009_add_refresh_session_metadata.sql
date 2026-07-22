-- Migration: 009_add_refresh_session_metadata
-- Description: Add throttled activity and client metadata to existing refresh-token families
-- Depends on: cloud-auth 006_refresh_tokens_table

ALTER TABLE auth.refresh_tokens
    ADD COLUMN IF NOT EXISTS user_agent VARCHAR(512),
    ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;

CREATE INDEX idx_refresh_tokens_user_family
    ON auth.refresh_tokens (user_id, family_id, issued_at DESC);
