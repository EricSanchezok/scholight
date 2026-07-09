"""Pydantic Settings for Scholight — all config via SCHOLIGHT_ env vars."""

import secrets

from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings with SCHOLIGHT_ prefix."""

    model_config = {
        "env_prefix": "SCHOLIGHT_",
        "env_file": ".env",
        "extra": "ignore",
        "case_sensitive": False,
    }

    # ── Storage ──
    data_root: str = "/inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/academic-data"

    # ── Zilliz Cloud ──
    zilliz_uri: str = "https://in05-d432d46d6c77308.serverless.ali-cn-hangzhou.cloud.zilliz.com.cn"
    zilliz_token: str = ""

    # ── Embedding (faro-hosted Qwen3-Embedding-0.6B) ──
    embedding_base_url: str = "https://faro-embedding.openapi-qb-nat.sii.edu.cn/v1"
    embedding_api_key: str = ""
    embedding_model: str = "qwen3-embedding-0.6b"
    embedding_dim: int = 1024
    embedding_batch_size: int = 512
    embedding_concurrency: int = 8

    # ── MinerU API ──
    mineru_api_key: str = ""

    # ── Search — Hybrid weights (paper-level, dense + BM25) ──
    search_hybrid_dense_weight: float = 0.60
    search_hybrid_bm25_weight: float = 0.40

    # ── Search — Phase 2.5 Rocchio query expansion hyperparameters ──
    search_rocchio_pos_k: int = 3
    search_rocchio_max_terms: int = 8
    search_rocchio_idf_floor: float = 3.5
    search_rocchio_max_query_len: int = 512
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
    bm25_coarse_top_k: int = 200
    dense_refine_top_k: int = 1024

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
    pg_host: str = "sii-pg.cf0m0gegaz1c.ap-east-1.rds.amazonaws.com"
    pg_port: int = 5432
    pg_database: str = "postgres"
    pg_user: str = "postgres"
    pg_password: str = ""
    pg_ssl_root_cert: str = "global-bundle.pem"
    pg_pool_min_size: int = 5
    pg_pool_max_size: int = 20
    pg_pool_acquire_timeout: float = 5.0
    pg_pool_command_timeout: float = 10.0
    pg_pool_max_inactive_lifetime: float = 300.0

    # ── JWT ──
    auth_jwt_secret: str = secrets.token_urlsafe(64)
    jwt_secret: str = ""
    jwt_access_token_ttl_minutes: int = 15
    jwt_refresh_token_ttl_days: int = 7

    @model_validator(mode="after")
    def _sync_jwt_secret(self) -> "Settings":
        # Canonical field is auth_jwt_secret (via SCHOLIGHT_AUTH_JWT_SECRET).
        # Always sync jwt_secret ← auth_jwt_secret so downstream code reads
        # the correct key regardless of which field it uses.
        self.jwt_secret = self.auth_jwt_secret
        return self

    # ── Auth ──
    account_lockout_threshold: int = 5
    account_lockout_duration_minutes: int = 15

    # ── Aliyun DirectMail ──
    aliyun_dm_access_key_id: str = ""
    aliyun_dm_access_key_secret: str = ""
    aliyun_dm_account_name: str = ""
    aliyun_dm_from_alias: str = "Scholight"
    aliyun_dm_reply_to_address: bool = True

    # ── CORS ──
    cors_allow_origins: list[str] = ["*"]
    # ── Server ──
    server_host: str = "0.0.0.0"
    server_port: int = 8000


settings = Settings()
