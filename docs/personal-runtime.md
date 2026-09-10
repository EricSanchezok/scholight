# Personal lean deployment

`python scripts/personal_runtime.py foundation` renders the isolated ECR, KMS,
Secrets Manager and GitHub publisher contract. `runtime` renders three independent
EC2 services (API, web and Web Extract), a bounded metadata task and a separate
product migration task. Templates make no AWS calls and require an explicit
expected account. Image parameters require immutable digests. Keep actual account
resources and populated parameters in operator configuration, never source files.

## Ownership and authentication

Platform owns the host, private network and ingress. This repository owns all
Scholight task definitions, roles, secrets and its background registration. The API
and metadata task use separate Zilliz credentials: query-only and data read/write,
respectively. Both retain the deployed Qwen embedding model and dimensions. Never
substitute a personal administrator key or silently fall back to the company URI.

Preserve production JWT, Access Key HMAC, anonymous quota HMAC and MCP delegation
values during cutover. Copy only required provider fields through a controlled
process to Secrets Manager. No platform DeepSeek fallback is provisioned. Runtime
and migration PostgreSQL roles remain separate; the migration command only owns
`scholight.*`. Apply additive migrations before enabling metadata sync.

## Ports, resource isolation and probes

API listens on 8000 (host 18200), web on 8080 (host 13200), and Extract on 8001
(host 18201). The shared edge terminates TLS and strips `/api` before forwarding
API requests, including the exact `/api/mcp` route. Internal Extract is private.
The web image uses `SCHOLIGHT_API_UPSTREAM` (default `api:8000`) through the nginx
template entrypoint; personal deployment points it at the host's private address.

Each service has its own execution and task roles, log group, health check and
stop-before-replace deployment. Liveness probes use `/livez`; dependency failures
belong to readiness and do not restart otherwise healthy API processes. API and
Extract memory ceilings are initially 768 MiB each, with web at 128 MiB. Validate
representative real traffic before adoption and stop admission on a failed capacity
gate. Metadata has a 768 MiB task ceiling, one embedding request at a time and
64-paper batches. PostgreSQL pools are limited to three API and two metadata
connections. All logs expire after seven days.

## Migration and operation

Publish merged revisions manually using `publish-personal.yml`; verify CI and
native ARM64 startup before using the resulting digests. `store migrate` runs in
the private ECS task using only the migrator secret. It never imports an old
snapshot or migrates `auth`. Keep metadata schedules disabled until source freeze,
current cursor and target paper count are verified. Record the before/after cursor
and deferred-fulltext counts for the catch-up run.

The metadata advisory lock pins every database operation, including deferred-ledger
batch writes, to the same PostgreSQL session. A failed batch must propagate out of
that session without advancing the daily cursor. Re-run the unchanged day after
repair; the abstract upsert and recovery ledger are idempotent, so a vector write
that succeeded before a database failure does not require deleting target data.

Configure product background registration for 08:00 UTC after isolated search,
login, Access Key, MCP and Extract tests. Disable competing old triggers before
admission, switch the public edge/DNS, then scale only the old Scholight services
to zero. Retain full-text and Survey definitions and historical data. A rollback
stops new synchronization before restoring the previous ingress; shared PostgreSQL
must not be replaced with an old snapshot. Once the personal collection advances,
do not resume company-collection writes without explicit reconciliation.

`python scripts/personal_control.py` renders separately bootstrapped OIDC and
CloudFormation service roles. Use the repository's actual immutable OIDC subject
prefix and protect `personal-image-publish`, `personal-infrastructure`,
`personal-preview`, and `personal-database` environments to main. No application
secrets are readable by the GitHub control roles. `personal-runtime.yml` only runs
by manual dispatch: `plan` uses the non-secret parameter variable, `apply` requires
the reviewed exact change-set ARN, and `migrate` launches only the registered
migration task on the private EC2 cluster. It neither opens a database port to the
runner nor registers arbitrary migration images. The guard rejects resource removal
and replacement other than immutable ECS task definitions.
