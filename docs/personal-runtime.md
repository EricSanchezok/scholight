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
2. For the initial des adoption, supply the immutable `binding_key` described in
   `des-restoration.md`. The first binding always disables both write consumers.
   Run `personal-runtime.yml` with `operation=plan` and the exact
   `release_manifest_key` printed by publication. Review the account, region,
   source revision, image changes, resource changes and exact change set ARN.
3. Run the same workflow with `operation=apply`, the same manifest key and the
   reviewed change set ARN and the same binding key. Main and PR CI never publish
   or deploy. The plan pins every runtime parameter, credential version and binding
   checksum. Apply rechecks secret-version availability without reading values.
4. After abstract reconciliation and `scheduler adopt-baseline`, use a new plan
   with `resume_ingestion=true` and the emitted `recovery/des/.../adoption.json`
   key. The proof must match the exact target ID. Apply pins and rechecks its hash.
   A version 1 application rollback preserves des while selecting lean mode and
   pausing both consumers; it never enables legacy writes against a new cursor.

A version 2 manifest contains all five immutable image digests, architecture, source and
controller commits, the pinned Identity revision and checksums of every immutable
product migration. It is persisted at `releases/<sha>/arm64-<run>-<attempt>.json`;
existing keys cannot be overwritten by publication. Apply verifies the byte hash
bound to the plan, account, region and current reviewed controller. Unstarted plans
expire after 24 hours or any intervening runtime stack update.

The current deployed image's immutable SHA tag proves its source migration
contract. Ordinary releases require identical contracts. The only reviewed N-1 exception is
migration 016, whose exact checksum is registered in `personal_compatibility.py`.
Changed applied checksums, other additions/removals and Identity revisions fail
closed. Run `operation=migrate` with the candidate manifest to execute this
append before application adoption; it uses the candidate API digest with the
existing private migration network, credentials and dedicated roles. It pauses
both consumers and records a pinned ECS launch before execution. A successful
exit writes `compatibility/<contract-hash>/migration.json`; failed or uncertain
execution never produces that proof. Repeated execution resumes the same launch
within its bounded idempotency window. Investigate an older unconfirmed launch
instead of starting a second migration. Consumers stay paused after migration.
The N-1 test runs the unchanged legacy queue/cursor facade against schema 016;
retire this exception after all retained rollback images adopt 016. Never migrate
Identity or apply a destructive down migration here.

Apply pauses `scholight-metadata` and `scholight-ingest`, waits for the host's acknowledgement of
that exact SSM parameter version, and waits for actual ECS task termination, including
STOPPING tasks. It never kills an active sync. Waiting expires after twenty minutes
with admission paused and a durable continuation record. The frozen reviewed
runtime template and parameters are replayed into a new change set because pausing
this same stack invalidates its earlier change sets. Only the two reviewed admission flags set to `false`
may differ; the generated ARN is logged and its template/parameters are rechecked.
After application and task/grant revisions converge, the reviewed admission state
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

API, Web, Extract, metadata/ingest task definitions and admission grants belong to this
product stack. Account Center, Scholens, PostgreSQL, Valkey, edge and host resources
are outside it and must retain their task identities during an application release.
All personal environments are production environments restricted to main, despite
retained `personal-preview` names used in immutable OIDC subjects.

## Runtime configuration and recovery reference

# Personal lean deployment

`python scripts/personal_runtime.py foundation` renders the isolated ECR, KMS,
Secrets Manager and GitHub publisher contract. `runtime` renders three independent
EC2 services (API, web and Web Extract), bounded metadata and fulltext tasks and a separate
product migration task. Templates make no AWS calls and require an explicit
expected account. Image parameters require immutable digests. Keep actual account
resources and populated parameters in operator configuration, never source files.

## Ownership and authentication

Platform owns the host, private network and ingress. This repository owns all
Scholight task definitions, roles, secrets and its background registration. The API, metadata and fulltext tasks use independent Zilliz credentials, with
read access for the API and the required data mutations for each worker. All retain the deployed Qwen embedding model and dimensions. Never
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
connections. All logs expire after seven days. Fulltext is an hourly admitted task, not an ECS
service: it exits within thirty minutes, uses at most 2,048 MiB and 512 CPU units,
and opens at most two database connections. Embedding runs in one lane with
64 chunks per request. Its role can only read/write the dedicated encrypted
`recovery/des/` prefix; restoration objects and noncurrent versions expire after
30 days. Ingest admission defaults to disabled until baseline and canary verification.

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
the reviewed exact change-set ARN, and `migrate` launches the reviewed candidate
migration task on the private EC2 cluster. It neither opens a database port to the
runner. Candidate migration images must come from a verified merged production
manifest, and the cloned task retains only the existing private migrator roles. The guard rejects resource removal
and replacement other than immutable ECS task definitions.

The CloudFormation role authorizes `ecs:DeregisterTaskDefinition` against `*`,
restricted to the destination region: ECS does not support task-definition ARN
authorization for this cleanup action. Keeping it in a family-scoped statement
leaves successful service updates stuck cleaning up previous revisions. Service
updates, tagging and role passing remain separately restricted to this product.

Publication always runs the controller from the reviewed workflow commit, even
when building another merged application SHA. New version 2 manifests require
that source's `deploy/personal/image-contract.json`; revisions predating the
complete destination-aware contract must use their retained version 1 rollback
manifests instead. This prevents a newly built legacy sync image from being
mislabelled as a destination-aware consumer.

The hourly fulltext registration uses Platform admission priority 1 (the protocol
accepts only 0 and 1). Fresh/revision versus aged historical ordering is enforced
inside Scholight's target queue, independently of the host admission priority.
