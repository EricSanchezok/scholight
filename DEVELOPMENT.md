# Local development

Scholight follows the cross-product shared-local contract in the
[`sanchezcloud-identity` handbook](https://github.com/EricSanchezok/sanchezcloud-identity/blob/main/docs/guides/local-development.md).
The normal topology is local Frontend and API, shared local PostgreSQL and MinIO, and remote
Zilliz for read-only paper search.

## Registered ports

| Service | Local host port | Notes |
| --- | ---: | --- |
| Frontend | 7200 | Canonical browser origin |
| API | 7201 | Vite proxies relative `/api` requests here |
| Extract | 7202 | Optional host-side debug process only |
| PostgreSQL | 55432 | Shared `sanchezcloud` database |
| MinIO | 59000 / 59001 | S3 API / console |

All local URLs and bindings use `127.0.0.1`. Vite uses strict-port mode and startup must fail when
a registered port is occupied. Container-internal and production ports are unchanged.

## First-time setup

Start and provision the shared PostgreSQL and MinIO stack using the Identity/Account Center
shared-local workflow. Identity migrates only `auth`; a `scholight_migrator` then explicitly
migrates only `scholight`. The API always runs as `scholight_app` and never owns or migrates a
schema.

Create the private local catalog and install dependencies:

```bash
cp .env.example .env.local
chmod 600 .env.local
uv sync --all-extras
npm --prefix frontend install
```

Fill `.env.local` with a collection-scoped read-only Zilliz token, local runtime database
password, and required local-only application secrets. The checked-in catalog deliberately names
no RDS host. Do not reuse the legacy root `.env` for local application startup.

Apply a pending product migration only through an explicit command using a temporary environment
file containing the `scholight_migrator` credentials:

```bash
SCHOLIGHT_DISABLE_DOTENV=1 \
  uv run --env-file .env.local.migrate scholight store migrate
```

Never put administrator credentials in either file. Reapply runtime grants and run the Identity
`product-runtime` role audit after a migration.

## Daily core startup

```bash
./scripts/dev.sh
```

The script loads `.env.local` explicitly, disables implicit root `.env` loading, rejects any
PostgreSQL host other than `127.0.0.1:55432`, and starts only Frontend and API. It never installs,
migrates, repairs grants, or starts ingestion.

Optional profiles are separate processes:

- `extract`: run the Extract sidecar on `127.0.0.1:7202` only when testing extraction.
- `survey`: run Survey draft/worker processes against local PostgreSQL and MinIO. Survey
  chart rendering additionally needs a local `dot` binary — install with
  `brew install graphviz` on macOS (the production image ships it).
  When both HTML and PDF evidence are available for a paper, the HTML extract is authoritative;
  the PDF path is a fallback only after HTML retrieval fails.
- ingestion and maintenance: never part of shared-local development.

## Remote dependency policy

Remote Zilliz is allowed only for paper search with a collection-scoped read-only credential.
Local development must not run metadata sync, paper ingest, backfill, collection init/drop/restore,
`health --fix`, or any other Zilliz write/repair operation. Embedding, DeepSeek, MinerU, and image
generation are explicit acceptance-test dependencies; unit and CI tests use fakes.

Local mode must never connect to RDS, production S3, production mail, KMS, Redis, or RabbitMQ.
Production commands use the protected ECS workflows and contracts documented in `deploy/ecs`;
changing `.env.local` is not an approved route to a remote stateful service.

## Shutdown and diagnostics

Stop the foreground `./scripts/dev.sh` process with Ctrl-C; it terminates both child processes.
Use `lsof -nP -iTCP:7200 -sTCP:LISTEN` and the equivalent check for `7201` when a port is occupied.
Use `uv run scholight store health` only with the read-only Zilliz credential and without repair
flags.
