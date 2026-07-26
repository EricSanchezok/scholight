"""Pydantic Settings for Scholight — all config via SCHOLIGHT_ env vars."""

import os

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

AUTH_CLIENT_ID = "scholight"
_ENV_FILE = None if os.environ.get("SCHOLIGHT_DISABLE_DOTENV") == "1" else ".env"


class Settings(BaseSettings):
    """Application settings with SCHOLIGHT_ prefix."""

    model_config = {
        "env_prefix": "SCHOLIGHT_",
        "env_file": _ENV_FILE,
        "extra": "ignore",
        "case_sensitive": False,
    }

    # ── Storage ──
    data_root: str = "/data"

    # ── Zilliz Cloud ──
    zilliz_uri: str = ""
    zilliz_token: str = ""

    # ── Embedding (faro-hosted Qwen3-Embedding-0.6B) ──
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "qwen3-embedding-0.6b"
    embedding_dim: int = 1024
    embedding_batch_size: int = 512
    embedding_concurrency: int = 8

    # ── Native daily ingestion ──
    ingest_recent_days: int = Field(default=90, ge=7, le=365)
    metadata_sync_hour_utc: int = Field(default=8, ge=0, le=23)
    ingest_max_attempts: int = Field(default=8, ge=1, le=32)
    ingest_lease_seconds: int = Field(default=7200, ge=300, le=86400)

    # ── MinerU API ──
    mineru_api_key: str = ""

    # ── Search — Level 1 paper recall ──
    # Fixed recall depth keeps ranking stable when callers change response size.
    search_paper_candidate_top_k: int = Field(default=200, ge=1)

    # ── Search — Hybrid weights (paper-level, dense + BM25) ──
    search_hybrid_dense_weight: float = 0.60
    search_hybrid_bm25_weight: float = 0.40

    # ── Search — Phase 3 abstract-length quality penalty ──
    search_abstract_len_midpoint: int = 120
    search_abstract_len_steepness: float = 10.0

    # ── Search — Level 2 chunk aggregation (C1: MaxP/SumP blend) ──
    # 0.0 = pure SumP (avg of top-5 chunks), 1.0 = pure MaxP.
    # 0.5 balances both signals — Nardini et al. SIGIR 2025.
    search_chunk_aggregation_alpha: float = 0.5
    # ── Search — Level 2 position weighting (C3) ──
    # 0.0 = no position boost, >0 = boost later chunks (results/conclusion).
    search_position_weight_beta: float = 0.3
    # ── Level 2 chunk search sizes ──
    bm25_coarse_top_k: int = 30
    dense_refine_top_k: int = 256
    # Public-result enrichment stays short because it degrades without failing the search.
    search_enrichment_rpc_timeout_seconds: float = 1.5
    # Strict Level 2 must cover a cold 172M-row chunk index while remaining bounded.
    # Each blocking Zilliz RPC is shorter than the end-to-end Level 2 deadline.
    search_level2_rpc_timeout_seconds: float = 45.0
    search_level2_timeout_seconds: float = 60.0
    background_queue_max_size: int = Field(default=512, ge=1, le=10000)

    # Reuse connections without introducing a new request-admission boundary.
    embedding_max_keepalive_connections: int = Field(default=20, ge=0, le=512)

    # ── Search — Level 2 RRF fusion (C2) ──
    search_rrf_k: int = 60
    search_rrf_paper_weight: float = 0.5
    search_rrf_chunk_weight: float = 0.5

    # ── Search — AUTOINDEX level ──
    # AUTOINDEX level 3 ≈ 95% recall; tuning target — bump to 5 if recall regressions observed
    search_level: int = 3
    chunk_search_level: int = 3

    # ── Logging ──
    log_level: str = "INFO"
    log_json: bool | None = None

    # ── PostgreSQL ──
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_database: str = "scholight"
    pg_user: str = "scholight"
    pg_password: str = ""
    pg_ssl_root_cert: str = "global-bundle.pem"
    pg_pool_min_size: int = 5
    pg_pool_max_size: int = 20
    pg_pool_acquire_timeout: float = 5.0
    pg_pool_command_timeout: float = 10.0
    pg_pool_max_inactive_lifetime: float = 300.0

    # ── JWT ──
    auth_jwt_secret: str = ""
    jwt_secret: str = ""
    jwt_access_token_ttl_minutes: int = 15
    jwt_refresh_token_ttl_days: int = 7
    auth_refresh_cookie_secure: bool = True

    @model_validator(mode="after")
    def _sync_jwt_secret(self) -> "Settings":
        # Canonical field is auth_jwt_secret (via SCHOLIGHT_AUTH_JWT_SECRET).
        # Always sync jwt_secret ← auth_jwt_secret so downstream code reads
        # the correct key regardless of which field it uses.
        self.jwt_secret = self.auth_jwt_secret
        return self

    # ── Auth ──
    public_web_url: str = "http://127.0.0.1:5173"
    account_lockout_threshold: int = 5
    account_lockout_duration_minutes: int = 15

    # ── Personal access keys ──
    access_key_hmac_secret: str = ""
    mcp_delegation_jwt_secret: str = ""

    # ── Anonymous public search ──
    anonymous_rate_limit_per_minute: int = Field(default=30, gt=0)
    anonymous_standard_daily_limit: int = Field(default=100, gt=0)
    anonymous_thorough_daily_limit: int = Field(default=30, gt=0)
    anonymous_quota_hmac_secret: str = ""
    authenticated_standard_daily_limit: int = Field(default=1000, ge=0)
    authenticated_thorough_daily_limit: int = Field(default=1000, ge=0)

    # ── Aliyun DirectMail ──
    aliyun_dm_access_key_id: str = ""
    aliyun_dm_access_key_secret: str = ""
    aliyun_dm_account_name: str = ""
    aliyun_dm_from_alias: str = "Scholight"
    aliyun_dm_reply_to_address: bool = True

    # ── CORS ──
    cors_allow_origins: list[str] = []
    # ── Server ──
    server_host: str = "127.0.0.1"
    server_port: int = 8000
    proxy_headers: bool = False
    forwarded_allow_ips: str = "127.0.0.1"
    server_keep_alive_seconds: int = Field(default=65, ge=1, le=300)
    server_limit_concurrency: int = Field(default=96, ge=1, le=4096)
    server_backlog: int = Field(default=128, ge=1, le=4096)


settings = Settings()


def validate_api_runtime_settings() -> None:
    """Validate secrets and trust boundaries required only by the HTTP API."""
    if len(settings.jwt_secret.strip().encode("utf-8")) < 32:
        raise ValueError("SCHOLIGHT_AUTH_JWT_SECRET must contain at least 32 UTF-8 bytes")
    if len(settings.anonymous_quota_hmac_secret.encode("utf-8")) < 32:
        raise ValueError(
            "SCHOLIGHT_ANONYMOUS_QUOTA_HMAC_SECRET must contain at least 32 UTF-8 bytes"
        )
    if len(settings.access_key_hmac_secret.encode("utf-8")) < 32:
        raise ValueError("SCHOLIGHT_ACCESS_KEY_HMAC_SECRET must contain at least 32 UTF-8 bytes")
    if len(settings.mcp_delegation_jwt_secret.encode("utf-8")) < 32:
        raise ValueError("SCHOLIGHT_MCP_DELEGATION_JWT_SECRET must contain at least 32 UTF-8 bytes")
    if settings.proxy_headers and settings.forwarded_allow_ips.strip() == "*":
        raise ValueError(
            "SCHOLIGHT_FORWARDED_ALLOW_IPS must not be '*' when proxy headers are enabled"
        )
    if "*" in settings.cors_allow_origins:
        raise ValueError("SCHOLIGHT_CORS_ALLOW_ORIGINS must list explicit origins for the API")
    if not settings.zilliz_uri.strip():
        raise ValueError("SCHOLIGHT_ZILLIZ_URI is required by the search API")
    if not settings.zilliz_token.strip():
        raise ValueError("SCHOLIGHT_ZILLIZ_TOKEN is required by the search API")
    if not settings.embedding_base_url.strip():
        raise ValueError("SCHOLIGHT_EMBEDDING_BASE_URL is required by the search API")
