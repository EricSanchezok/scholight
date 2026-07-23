-- Scholight product-data baseline.
-- Shared identity/session tables remain exclusively in the auth schema.

CREATE TABLE scholight.user_profiles (
    user_id       BIGINT PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    status        TEXT NOT NULL DEFAULT 'active',
    is_admin      BOOLEAN NOT NULL DEFAULT FALSE,
    blocked_at    TIMESTAMPTZ,
    block_reason  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT user_profiles_status
        CHECK (status IN ('active', 'blocked')),
    CONSTRAINT user_profiles_block_state
        CHECK (
            (status = 'active' AND blocked_at IS NULL AND block_reason IS NULL)
            OR (status = 'blocked' AND blocked_at IS NOT NULL)
        )
);

CREATE TABLE scholight.user_quota_overrides (
    user_id      BIGINT NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    strength     TEXT NOT NULL,
    daily_limit  INTEGER NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, strength),
    CONSTRAINT user_quota_overrides_strength
        CHECK (strength IN ('standard', 'thorough')),
    CONSTRAINT user_quota_overrides_daily_limit
        CHECK (daily_limit BETWEEN 0 AND 1000000)
);

CREATE TABLE scholight.admin_audit_events (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id          UUID NOT NULL UNIQUE,
    actor_type        TEXT NOT NULL,
    actor_user_id     BIGINT REFERENCES auth.users(id) ON DELETE SET NULL,
    actor_identifier  TEXT NOT NULL,
    target_user_id    BIGINT REFERENCES auth.users(id) ON DELETE SET NULL,
    target_email      TEXT NOT NULL,
    action            TEXT NOT NULL,
    before_state      JSONB NOT NULL,
    after_state       JSONB NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT admin_audit_events_actor_type
        CHECK (actor_type IN ('user', 'cli')),
    CONSTRAINT admin_audit_events_actor_identity
        CHECK (
            (actor_type = 'user' AND actor_user_id IS NOT NULL)
            OR (actor_type = 'cli' AND actor_user_id IS NULL)
        ),
    CONSTRAINT admin_audit_events_action
        CHECK (action IN ('quota_overrides_updated', 'admin_granted', 'admin_revoked'))
);

CREATE INDEX admin_audit_events_created_idx
    ON scholight.admin_audit_events (created_at DESC, id DESC);

CREATE TABLE scholight.user_daily_search_usage (
    quota_date  DATE NOT NULL,
    user_id     BIGINT NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    strength    TEXT NOT NULL,
    used_count  INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (quota_date, user_id, strength),
    CONSTRAINT user_daily_search_usage_strength
        CHECK (strength IN ('standard', 'thorough')),
    CONSTRAINT user_daily_search_usage_used_count
        CHECK (used_count >= 0)
);

CREATE TABLE scholight.anonymous_daily_search_usage (
    quota_date  DATE NOT NULL,
    ip_digest   BYTEA NOT NULL,
    strength    TEXT NOT NULL,
    used_count  INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (quota_date, ip_digest, strength),
    CONSTRAINT anonymous_daily_search_usage_digest
        CHECK (octet_length(ip_digest) = 32),
    CONSTRAINT anonymous_daily_search_usage_strength
        CHECK (strength IN ('standard', 'thorough')),
    CONSTRAINT anonymous_daily_search_usage_used_count
        CHECK (used_count >= 0)
);

CREATE TABLE scholight.search_history (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id           BIGINT NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    query_text        TEXT NOT NULL,
    strength          TEXT NOT NULL,
    filters           JSONB,
    result_count      INTEGER NOT NULL DEFAULT 0,
    response_time_ms  REAL NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at        TIMESTAMPTZ,
    CONSTRAINT search_history_strength
        CHECK (strength IN ('standard', 'thorough')),
    CONSTRAINT search_history_result_count
        CHECK (result_count >= 0),
    CONSTRAINT search_history_response_time
        CHECK (response_time_ms >= 0)
);

CREATE INDEX search_history_user_time_idx
    ON scholight.search_history (user_id, created_at DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX search_history_created_idx
    ON scholight.search_history (created_at);

CREATE TABLE scholight.access_keys (
    id            UUID PRIMARY KEY,
    user_id       BIGINT NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    name          VARCHAR(64) NOT NULL,
    key_prefix    VARCHAR(32) NOT NULL UNIQUE,
    key_last4     VARCHAR(4) NOT NULL,
    key_digest    BYTEA NOT NULL UNIQUE,
    scopes        TEXT[] NOT NULL DEFAULT ARRAY['search']::TEXT[],
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ,
    expires_at    TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ,
    CONSTRAINT access_keys_name_not_blank
        CHECK (btrim(name) <> ''),
    CONSTRAINT access_keys_digest_length
        CHECK (octet_length(key_digest) = 32),
    CONSTRAINT access_keys_last4_length
        CHECK (char_length(key_last4) = 4),
    CONSTRAINT access_keys_search_scope_only
        CHECK (scopes = ARRAY['search']::TEXT[])
);

CREATE INDEX access_keys_user_created_idx
    ON scholight.access_keys (user_id, created_at DESC);

CREATE INDEX access_keys_user_active_idx
    ON scholight.access_keys (user_id)
    WHERE revoked_at IS NULL;

CREATE TABLE scholight.usage_events (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_id          VARCHAR(128) NOT NULL UNIQUE,
    user_id             BIGINT NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    strength            TEXT NOT NULL,
    actor_type          TEXT NOT NULL,
    access_key_id       UUID REFERENCES scholight.access_keys(id) ON DELETE SET NULL,
    outcome             TEXT NOT NULL,
    quota_units         INTEGER NOT NULL,
    result_count        INTEGER,
    search_duration_ms  REAL,
    status_code         INTEGER,
    error_code          VARCHAR(64),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT usage_events_strength
        CHECK (strength IN ('standard', 'thorough')),
    CONSTRAINT usage_events_actor_type
        CHECK (actor_type IN ('web', 'access_key')),
    CONSTRAINT usage_events_outcome
        CHECK (outcome IN ('success', 'degraded', 'failed')),
    CONSTRAINT usage_events_quota_units
        CHECK (quota_units >= 0),
    CONSTRAINT usage_events_result_count
        CHECK (result_count IS NULL OR result_count >= 0),
    CONSTRAINT usage_events_duration
        CHECK (search_duration_ms IS NULL OR search_duration_ms >= 0),
    CONSTRAINT usage_events_key_actor
        CHECK (
            (actor_type = 'access_key' AND access_key_id IS NOT NULL)
            OR (actor_type = 'web' AND access_key_id IS NULL)
        )
);

CREATE INDEX usage_events_user_created_idx
    ON scholight.usage_events (user_id, created_at DESC, id DESC);

CREATE INDEX usage_events_user_strength_created_idx
    ON scholight.usage_events (user_id, strength, created_at);

CREATE INDEX usage_events_user_key_created_idx
    ON scholight.usage_events (user_id, access_key_id, created_at);
