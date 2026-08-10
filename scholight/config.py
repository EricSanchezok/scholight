"""Pydantic Settings for Scholight — all config via SCHOLIGHT_ env vars."""

import os
from typing import Literal
from urllib.parse import urlsplit

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
    pg_host: str = "127.0.0.1"
    pg_port: int = 55432
    pg_database: str = "sanchezcloud"
    pg_user: str = "scholight_app"
    pg_password: str = ""
    pg_ssl_root_cert: str = "disable"
    pg_pool_min_size: int = 2
    pg_pool_max_size: int = 8
    pg_pool_acquire_timeout: float = 5.0
    pg_pool_command_timeout: float = 10.0
    pg_pool_max_inactive_lifetime: float = 300.0

    # ── Shared SanchezCloud avatar (read-only in Scholight) ──
    avatar_s3_bucket: str = ""
    avatar_s3_endpoint_url: str | None = None
    avatar_url_ttl_seconds: int = Field(default=900, ge=60, le=3600)

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
    public_web_url: str = "http://127.0.0.1:7200"
    account_lockout_threshold: int = 5
    account_lockout_duration_minutes: int = 15

    # ── Personal access keys ──
    access_key_hmac_secret: str = ""
    mcp_delegation_jwt_secret: str = ""

    # ── Web Extract ──
    extract_enabled: bool = False
    extract_service_url: str = "http://127.0.0.1:7202"
    extract_internal_token: str = ""
    extract_request_timeout_seconds: float = Field(default=55.0, ge=1.0, le=180.0)
    extract_fetch_timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)
    extract_render_timeout_seconds: float = Field(default=45.0, ge=1.0, le=180.0)
    extract_max_download_bytes: int = Field(default=50_000_000, ge=1024, le=250_000_000)
    extract_cache_ttl_seconds: int = Field(default=600, ge=1, le=86400)
    extract_cache_max_bytes: int = Field(
        default=256 * 1024 * 1024,
        ge=1024 * 1024,
        le=2 * 1024 * 1024 * 1024,
    )
    extract_static_concurrency: int = Field(default=16, ge=1, le=256)
    extract_browser_concurrency: int = Field(default=2, ge=1, le=32)
    extract_server_host: str = "127.0.0.1"
    extract_server_port: int = Field(default=7202, ge=1, le=65535)

    # ── Survey ──
    # Provider-standard names intentionally remain unprefixed end to end.
    deepseek_api_key: str = Field(default="", validation_alias="DEEPSEEK_API_KEY")
    survey_title_api_url: str = "https://api.deepseek.com/chat/completions"
    survey_title_timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0)
    image_gen_api_key: str = Field(default="", validation_alias="IMAGE_GEN_API_KEY")
    survey_mcp_jwt_secret: str = ""
    survey_mcp_url: str = "http://api:8000/mcp"
    survey_s3_bucket: str = ""
    survey_s3_endpoint_url: str | None = None
    survey_s3_public_endpoint_url: str | None = None
    # Workers can be exercised with real dependencies while the public product
    # remains fail-closed. Survey has never shipped, so there is no legacy switch.
    survey_runtime_enabled: bool = False
    survey_public_mode: Literal["off", "all"] = "off"
    survey_daily_limit: int = Field(default=3, ge=1, le=100)
    survey_draft_timeout_seconds: int = Field(default=1800, ge=60, le=3600)
    survey_job_timeout_seconds: int = Field(default=86400, ge=60, le=172800)
    survey_provider_max_attempts: int = Field(default=3, ge=1, le=5)
    survey_provider_retry_base_seconds: float = Field(default=2.0, ge=0.0, le=30.0)
    survey_provider_retry_max_seconds: float = Field(default=30.0, ge=0.0, le=120.0)
    survey_draft_global_concurrency: int = Field(default=64, ge=1, le=64)
    survey_job_global_concurrency: int = Field(default=16, ge=1, le=16)
    survey_draft_per_user_concurrency: int = Field(default=8, ge=1, le=64)
    survey_job_per_user_concurrency: int = Field(default=4, ge=1, le=16)
    survey_draft_worker_concurrency: int = Field(default=8, ge=1, le=64)
    survey_job_worker_concurrency: int = Field(default=2, ge=1, le=16)
    survey_heartbeat_seconds: int = Field(default=15, ge=5, le=60)
    survey_lease_seconds: int = Field(default=120, ge=30, le=600)

    @model_validator(mode="after")
    def _validate_survey_concurrency(self) -> "Settings":
        mcp_url = urlsplit(self.survey_mcp_url)
        if (
            mcp_url.scheme not in {"http", "https"}
            or not mcp_url.hostname
            or mcp_url.username is not None
            or mcp_url.password is not None
            or bool(mcp_url.query)
            or bool(mcp_url.fragment)
        ):
            raise ValueError(
                "SCHOLIGHT_SURVEY_MCP_URL must be an HTTP(S) URL without credentials, "
                "query parameters, or a fragment"
            )
        if self.survey_provider_retry_base_seconds > self.survey_provider_retry_max_seconds:
            raise ValueError(
                "SCHOLIGHT_SURVEY_PROVIDER_RETRY_BASE_SECONDS must not exceed "
                "SCHOLIGHT_SURVEY_PROVIDER_RETRY_MAX_SECONDS"
            )
        if self.survey_draft_per_user_concurrency > self.survey_draft_global_concurrency:
            raise ValueError(
                "SCHOLIGHT_SURVEY_DRAFT_PER_USER_CONCURRENCY must not exceed "
                "SCHOLIGHT_SURVEY_DRAFT_GLOBAL_CONCURRENCY"
            )
        if self.survey_job_per_user_concurrency > self.survey_job_global_concurrency:
            raise ValueError(
                "SCHOLIGHT_SURVEY_JOB_PER_USER_CONCURRENCY must not exceed "
                "SCHOLIGHT_SURVEY_JOB_GLOBAL_CONCURRENCY"
            )
        if self.survey_draft_worker_concurrency > self.survey_draft_global_concurrency:
            raise ValueError(
                "SCHOLIGHT_SURVEY_DRAFT_WORKER_CONCURRENCY must not exceed "
                "SCHOLIGHT_SURVEY_DRAFT_GLOBAL_CONCURRENCY"
            )
        if self.survey_job_worker_concurrency > self.survey_job_global_concurrency:
            raise ValueError(
                "SCHOLIGHT_SURVEY_JOB_WORKER_CONCURRENCY must not exceed "
                "SCHOLIGHT_SURVEY_JOB_GLOBAL_CONCURRENCY"
            )
        if self.survey_heartbeat_seconds * 2 >= self.survey_lease_seconds:
            raise ValueError(
                "SCHOLIGHT_SURVEY_LEASE_SECONDS must exceed twice "
                "SCHOLIGHT_SURVEY_HEARTBEAT_SECONDS"
            )
        return self

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
    server_port: int = 7201
    proxy_headers: bool = False
    forwarded_allow_ips: str = "127.0.0.1"
    server_keep_alive_seconds: int = Field(default=65, ge=1, le=300)
    # Last-resort host guard. This is a coarse ASGI-task ceiling, not search capacity.
    server_limit_concurrency: int | None = Field(default=96, ge=1, le=4096)
    server_backlog: int = Field(default=128, ge=1, le=4096)


settings = Settings()


def is_survey_runtime_enabled() -> bool:
    """Return whether Survey workers and their real dependencies may run."""
    return settings.survey_runtime_enabled


def get_survey_public_mode() -> Literal["off", "all"]:
    """Return the user-visible Survey mode."""
    return settings.survey_public_mode


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
    if settings.extract_enabled:
        _validate_extract_shared_settings()
    if get_survey_public_mode() == "all":
        if not is_survey_runtime_enabled():
            raise ValueError(
                "SCHOLIGHT_SURVEY_RUNTIME_ENABLED must be true when "
                "SCHOLIGHT_SURVEY_PUBLIC_MODE is 'all'"
            )
        if not settings.deepseek_api_key.strip():
            raise ValueError("DEEPSEEK_API_KEY is required when Survey is enabled")
        if len(settings.survey_mcp_jwt_secret.encode("utf-8")) < 32:
            raise ValueError("SCHOLIGHT_SURVEY_MCP_JWT_SECRET must contain at least 32 UTF-8 bytes")
        if not settings.survey_s3_bucket.strip():
            raise ValueError("SCHOLIGHT_SURVEY_S3_BUCKET is required when Survey is enabled")
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


def _validate_extract_shared_settings() -> None:
    if len(settings.extract_internal_token.encode("utf-8")) < 32:
        raise ValueError("SCHOLIGHT_EXTRACT_INTERNAL_TOKEN must contain at least 32 UTF-8 bytes")
    if not settings.extract_service_url.startswith(("http://", "https://")):
        raise ValueError("SCHOLIGHT_EXTRACT_SERVICE_URL must be an HTTP or HTTPS URL")


def validate_extract_runtime_settings() -> None:
    """Validate only configuration required by the internal Extract sidecar."""
    _validate_extract_shared_settings()


def validate_survey_worker_settings() -> None:
    """Validate only the secrets and storage needed by the Survey worker."""
    if not is_survey_runtime_enabled():
        raise ValueError("SCHOLIGHT_SURVEY_RUNTIME_ENABLED must be true to run the Survey worker")
    if not settings.deepseek_api_key.strip():
        raise ValueError("DEEPSEEK_API_KEY is required by the Survey worker")
    if len(settings.survey_mcp_jwt_secret.encode("utf-8")) < 32:
        raise ValueError("SCHOLIGHT_SURVEY_MCP_JWT_SECRET must contain at least 32 UTF-8 bytes")
    if not settings.survey_s3_bucket.strip():
        raise ValueError("SCHOLIGHT_SURVEY_S3_BUCKET is required by the Survey worker")
    if not settings.aliyun_dm_access_key_id.strip():
        raise ValueError("SCHOLIGHT_ALIYUN_DM_ACCESS_KEY_ID is required by the Survey worker")
    if not settings.aliyun_dm_access_key_secret.strip():
        raise ValueError("SCHOLIGHT_ALIYUN_DM_ACCESS_KEY_SECRET is required by the Survey worker")
    if not settings.aliyun_dm_account_name.strip():
        raise ValueError("SCHOLIGHT_ALIYUN_DM_ACCOUNT_NAME is required by the Survey worker")


def validate_survey_draft_worker_settings() -> None:
    """Validate the smaller secret boundary needed by the Draft worker."""
    if not is_survey_runtime_enabled():
        raise ValueError(
            "SCHOLIGHT_SURVEY_RUNTIME_ENABLED must be true to run the Survey Draft worker"
        )
    if not settings.deepseek_api_key.strip():
        raise ValueError("DEEPSEEK_API_KEY is required by the Survey Draft worker")
    if len(settings.survey_mcp_jwt_secret.encode("utf-8")) < 32:
        raise ValueError("SCHOLIGHT_SURVEY_MCP_JWT_SECRET must contain at least 32 UTF-8 bytes")
