# Personal production runtime

This is the canonical production deployment contract for Scholight in account
`669409472143`, Hyderabad (`ap-south-2`), cluster `sanchezcloud-personal`.
The historical Fargate workflows are archived in `deploy/legacy/workflows` and
cannot run from the normal Actions workflow directory.

## Manual release and rollback

1. Run `publish-personal.yml` from main with a merged source revision. The prepare
   job freezes the full commit SHA before quality checks and building. Only ARM64
   production manifests are uploaded to the retained, encrypted personal release
   bucket. AMD64 remains a compatibility build.
2. Run `personal-runtime.yml` with `operation=plan` and the exact
   `release_manifest_key` printed by publication. Review the account, region,
   source revision, image changes, resource changes and exact change set ARN.
3. Run the same workflow with `operation=apply`, the same manifest key and the
   reviewed change set ARN. Main and PR CI never publish or deploy.

A manifest contains all four immutable image digests, architecture, source and
controller commits, the pinned Identity revision and checksums of every immutable
product migration. It is persisted at `releases/<sha>/arm64-<run>-<attempt>.json`;
existing keys cannot be overwritten by publication. Apply verifies the byte hash
bound to the plan, account, region and current reviewed controller. Unstarted plans
expire after 24 hours or any intervening runtime stack update.

The current deployed image's immutable SHA tag proves its source migration
contract. Ordinary application releases and rollbacks require identical product
migration checksums and Identity revision. A changed contract fails closed and
requires an independently reviewed additive migration and compatibility procedure;
this flow never migrates Identity or assumes a destructive database rollback is safe.
The `migrate` operation only reapplies the currently registered product migrator.

Apply pauses only `scholight-metadata`, waits for the host's acknowledgement of
that exact SSM parameter version, and waits for actual ECS task termination, including
STOPPING tasks. It never kills an active sync. Waiting expires after twenty minutes
with admission paused and a durable continuation record. The frozen reviewed
runtime template and parameters are replayed into a new change set because pausing
this same stack invalidates its earlier change sets. Only `MetadataEnabled=false`
may differ; the generated ARN is logged and its template/parameters are rechecked.
After application and task/grant revisions converge, the original admission state
is restored. The daily 08:00 UTC schedule and database cursor remain unchanged.

Checkpoint lookup lists only the exact stage-key prefix before reading an existing
object. Missing state is distinct from permission denial; access failures always stop
the operation.

Stage records live under `cloudformation/personal/releases/<change-set-id>/` in the
release bucket. Retry the same apply after a transient failure; completed stages
are not repeated. Missing acknowledgement or external stack changes fail closed.
A failed release leaves consumers paused until repair or a compatible rollback.
For rollback, select the previous verified manifest and repeat plan/apply using the
current controller. If recovery began while admission was already paused, verify
and restore the original recorded admission state after compatible recovery.

API, Web, Extract, metadata task definition and admission grant belong to this
product stack. Account Center, Scholens, PostgreSQL, Valkey, edge and host resources
are outside it and must retain their task identities during an application release.
All personal environments are production environments restricted to main, despite
retained `personal-preview` names used in immutable OIDC subjects.

## Runtime configuration and recovery reference

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

`MetadataBatchSize` defaults to 64 and accepts 1–512 papers. For a large catch-up,
increase it through a reviewed change set only after measuring the task's actual
memory use. The 768 MiB task ceiling and single embedding request remain unchanged;
stop admission if the host capacity gate fails. This tunes bounded network writes,
not the daily cursor boundary: interrupted days must still replay completely.

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
by manual dispatch: `plan` reads current non-secret stack configuration and the selected S3 manifest, `apply` requires
the reviewed exact change-set ARN, and `migrate` launches only the registered
migration task on the private EC2 cluster. It neither opens a database port to the
runner nor registers arbitrary migration images. The guard rejects resource removal
and replacement other than immutable ECS task definitions.

The CloudFormation role authorizes `ecs:DeregisterTaskDefinition` against `*`,
restricted to the destination region: ECS does not support task-definition ARN
authorization for this cleanup action. Keeping it in a family-scoped statement
leaves successful service updates stuck cleaning up previous revisions. Service
updates, tagging and role passing remain separately restricted to this product.
