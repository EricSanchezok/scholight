-- Migration: 006_create_anonymous_daily_search_usage
-- Description: Atomic per-IP anonymous daily search quota counters
-- Depends on: none; auth.daily_usage is intentionally not reused

CREATE TABLE public.anonymous_daily_search_usage (
    quota_date   DATE NOT NULL,
    ip_digest    BYTEA NOT NULL,
    search_level SMALLINT NOT NULL,
    used_count   INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT anonymous_daily_search_usage_pkey
        PRIMARY KEY (quota_date, ip_digest, search_level),
    CONSTRAINT anonymous_daily_search_usage_ip_digest_length
        CHECK (octet_length(ip_digest) = 32),
    CONSTRAINT anonymous_daily_search_usage_search_level
        CHECK (search_level IN (1, 2)),
    CONSTRAINT anonymous_daily_search_usage_used_count
        CHECK (used_count >= 0)
);

COMMENT ON TABLE public.anonymous_daily_search_usage IS
    'Anonymous daily search quota counters keyed by UTC date and HMAC-SHA256 IP digest.';

COMMENT ON COLUMN public.anonymous_daily_search_usage.quota_date IS
    'UTC calendar date assigned by the atomic reserve statement.';

COMMENT ON COLUMN public.anonymous_daily_search_usage.ip_digest IS
    '32-byte HMAC-SHA256 digest; raw client IP is never stored.';
