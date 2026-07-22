-- Migration: 007_create_access_keys
-- Description: User-managed, search-only Scholight personal access keys
-- Depends on: cloud-auth 002_create_users

CREATE TABLE public.access_keys (
    id           UUID PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    name         VARCHAR(64) NOT NULL,
    key_prefix   VARCHAR(32) NOT NULL UNIQUE,
    key_last4    VARCHAR(4) NOT NULL,
    key_digest BYTEA NOT NULL UNIQUE,
    scopes       TEXT[] NOT NULL DEFAULT ARRAY['search']::TEXT[],
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    expires_at   TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ,

    CONSTRAINT access_keys_name_not_blank CHECK (btrim(name) <> ''),
    CONSTRAINT access_keys_digest_length CHECK (octet_length(key_digest) = 32),
    CONSTRAINT access_keys_last4_length CHECK (char_length(key_last4) = 4),
    CONSTRAINT access_keys_search_scope_only CHECK (scopes = ARRAY['search']::TEXT[])
);

CREATE INDEX idx_access_keys_user_created
    ON public.access_keys (user_id, created_at DESC);

CREATE INDEX idx_access_keys_user_active
    ON public.access_keys (user_id)
    WHERE revoked_at IS NULL;
