# Scholight

AI-focused academic paper search engine — arXiv as single data source, passage-level vector search and survey generation.

## Architecture

```
scholight/
│   ├── api/          FastAPI REST API (search, survey, auth via cloud-auth)
│   ├── clients/      HTTP clients (OSS, RDC, Gateway)
│   ├── search/       Multi-stage retrieval + rerank pipeline
│   ├── store/        Zilliz Cloud interaction layer (papers, chunks, citations)
│   ├── pipeline/     PDF parsing, chunking, embedding
│   ├── sources/      arXiv data connectors (bulk tar + OAI-PMH)
│   ├── scheduler/    Ingestion orchestration + daily sync
│   ├── cli/          Click CLI (search, service, store)
│   ├── models/       Pydantic data models
│   └── db/           PostgreSQL queries (search/survey history)
├── cloud-auth/       Shared auth SDK (independent repo — gitignored in scholight)
├── migrations/       PostgreSQL schema migrations
├── scripts/          Data ingestion + maintenance scripts
├── docker/           Docker deployment (API server)
└── tests/            Integration & unit tests
```

## Dependencies

- **cloud-auth** — shared user auth & quota system (private repo)
- **Zilliz Cloud** — managed vector database (Milvus-compatible)
- **PostgreSQL** — search/survey history + user data (via cloud-auth)
- **arXiv** — single data source (bulk PDF tar + OAI-PMH API)
- **Aliyun OSS** — survey output storage (via `alibabacloud-oss-v2`)
- **RDC** — Research Data Center (survey job queue + lifecycle backend)

## Quick Start

```bash
# Clone
git clone git@github.com:EricSanchezok/scholight.git
cd scholight

# Install (requires SSH key for cloud-auth private repo)
uv sync

export SCHOLIGHT_ZILLIZ_URI=https://in05-xxxxx.serverless.ali-cn-hangzhou.cloud.zilliz.com.cn
export SCHOLIGHT_ZILLIZ_TOKEN=your-api-token

# Run database migrations
uv run scholight store init       # Zilliz Cloud collections
# cloud-auth migrations: see cloud-auth README

# Start API server
uv run uvicorn scholight.api.app:create_app --factory --host 0.0.0.0 --port 8000
```

### Configuration

All settings are read via `SCHOLIGHT_`-prefixed env vars. Copy `.env.example`:
```bash
cp .env.example .env
```

Key variables:
| Variable | Required | Purpose |
|---|---|---|
| `SCHOLIGHT_PG_*` | ✅ | PostgreSQL connection |
| `SCHOLIGHT_ZILLIZ_*` | ✅ | Zilliz Cloud (vector DB) |
| `SCHOLIGHT_AUTH_JWT_SECRET` | ✅ | JWT signing key |
| `SCHOLIGHT_OSS_*` | ✅ | Aliyun OSS for survey output |
| `DEEPSEEK_API_KEY` | ✅ | Survey LLM — passed to RCM subprocess |
| `OPENAI_API_KEY` | — | Survey figure — optional, skips if missing |
| `SCHOLIGHT_SURVEY_*` | — | Survey pipeline tuning |

### Survey Output

Survey results are stored hierarchically on OSS:
```
sanchez-research/
└── surveys/{user_id}/{YYYY-MM}/{survey_id}/
    ├── 08_survey.md       ← final English survey
    └── 08_survey.zh.md    ← Chinese translation
```
Download via `GET /survey/{id}/download?lang=en|zh` — returns a time-limited presigned OSS URL.

## Service Endpoints

| Service | Port | Path |
|---|---|---|
| FastAPI server | 8000 | `/search`, `/survey`, `/auth/*`, `/user/*` |

## Docker Deployment

The API server can run in a single Docker container.  All databases (Zilliz Cloud,
AWS RDS PostgreSQL) are external services configured via environment variables.
The daily arXiv sync pipeline runs on the host instance — **never** in the container.

```bash
# 1. Copy and fill in the production env file
cp .env.production.example .env.production
# Edit .env.production — SCHOLIGHT_AUTH_JWT_SECRET, ZILLIZ_TOKEN, PG_PASSWORD etc.

# 2. Build
docker compose build

# 3. Start
docker compose --env-file .env.production up -d
```

The container bundles the AWS RDS SSL cert at `/etc/ssl/certs/global-bundle.pem`.
Survey runs in production mode only (delegated to Gateway + RDC); local survey
mode (RCM subprocess) is not supported inside Docker.

### What's NOT in Docker

| Component | Where | Why |
|---|---|---|
| arXiv paper sync | Host | Downloads PDFs to local disk (`SCHOLIGHT_DATA_ROOT`) |
| PDF daemon | Host | Converts PDFs to markdown on local disk |
| Markdown daemon | Host | Chunks and embeds on local disk |
| Chunk daemon | Host | Ingests to Zilliz Cloud from host |

These daemons need `curl`, `pandoc`, local PDF storage, and direct arXiv network
access — all best run on the host instance alongside the Docker container.

## Development

```bash
uv run pre-commit install            # Git hooks
uv run ruff check scholight/           # Lint
uv run mypy scholight                  # Type check
uv run pytest scholight/ -v            # Run tests
```

## License

Internal use — SanchezCloud
