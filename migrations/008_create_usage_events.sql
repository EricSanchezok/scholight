-- Migration: 008_create_usage_events
-- Description: Content-free product usage and server search-time analytics
-- Depends on: 007_create_access_keys, cloud-auth 002_create_users

CREATE TABLE public.usage_events (
    id                 BIGSERIAL PRIMARY KEY,
    request_id VARCHAR(128) NOT NULL UNIQUE,
    user_id            BIGINT NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    operation          VARCHAR(32) NOT NULL,
    strength           VARCHAR(16) NOT NULL,
    actor_type         VARCHAR(16) NOT NULL,
    access_key_id UUID REFERENCES public.access_keys(id) ON DELETE SET NULL,
    outcome            VARCHAR(16) NOT NULL,
    quota_units INTEGER NOT NULL,
    result_count       INTEGER,
    search_duration_ms REAL,
    status_code        INTEGER,
    error_code         VARCHAR(64),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT usage_events_operation CHECK (operation IN ('search_level1', 'search_level2')),
    CONSTRAINT usage_events_strength CHECK (strength IN ('standard', 'thorough')),
    CONSTRAINT usage_events_actor_type CHECK (actor_type IN ('web', 'access_key')),
    CONSTRAINT usage_events_outcome CHECK (outcome IN ('success', 'degraded', 'failed')),
    CONSTRAINT usage_events_quota_units CHECK (quota_units >= 0),
    CONSTRAINT usage_events_result_count CHECK (result_count IS NULL OR result_count >= 0),
    CONSTRAINT usage_events_duration CHECK (
        search_duration_ms IS NULL OR search_duration_ms >= 0
    ),
    CONSTRAINT usage_events_key_actor CHECK (
        (actor_type = 'access_key' AND access_key_id IS NOT NULL)
        OR (actor_type = 'web' AND access_key_id IS NULL)
    )
);

CREATE INDEX idx_usage_events_user_created
    ON public.usage_events (user_id, created_at DESC, id DESC);

CREATE INDEX idx_usage_events_user_operation_created
    ON public.usage_events (user_id, operation, created_at);

CREATE INDEX idx_usage_events_user_key_created
    ON public.usage_events (user_id, access_key_id, created_at);
