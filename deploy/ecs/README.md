# Scholight ECS production operations

This directory is the authoritative production deployment package for Scholight.
New releases run on the shared SanchezCloud Fargate platform in Singapore. The
former single-host package under `deploy/production/` is frozen and exists only
as a temporary rollback reference during migration.

## Runtime boundaries

Each workload has one explicit image and one responsibility. The Python
Dockerfile intentionally has no default final stage: every build must name its
target so a large Survey or ingestion runtime cannot leak into the Search API.

| Image | Docker target | Responsibility |
| --- | --- | --- |
| `sanchezcloud-scholight-web` | frontend image | Static React application |
| `sanchezcloud-scholight-api` | `api` | Public Search, identity, quota, history, Access Key, and MCP APIs |
| `sanchezcloud-scholight-extract` | extract image | Private Playwright/Chromium extraction service |
| `sanchezcloud-scholight-ingest` | `ingest` | Metadata sync and bounded paper-ingestion tasks |
| `sanchezcloud-scholight-survey` | `survey` | Survey draft/full workers and the pinned RCM runtime |

New Full tasks pre-render PDFs in a one-render child process. The child receives
only a local request-file path on argv, accepts only report-root and bundled
font file URLs, loads only images referenced by final Markdown, writes directly
to a file with a disk cache, and has independent wall-clock and address-space
limits. A timeout, backend failure, or child OOM removes the partial PDF but
does not block Markdown publication. The API retains its byte-render fallback
only for archives created before pre-rendering was introduced.

The API and Extract processes share only the lightweight models in
`scholight.web_extract.contracts`. Importing the API application must not load
the extraction engine or require Playwright, Chromium, or `markdownify`; CI
builds the minimal API image on every pull request and verifies that boundary
inside the resulting container.

The `survey` image now installs `poppler-utils`. A Full Survey worker verifies
both the pinned `accelerate` executable and `pdftotext` before it opens the
database or claims work. A missing reader is therefore a startup failure, never
an implicit downgrade to abstract-only research. The API and Extract targets
remain free of this dependency; Ingest retains its separate PDF fallback reader.

The Web service joins the shared Service Connect namespace as a client, while
the API and Extract services publish the `api` and `extract` discovery names.
Nginx therefore reaches `http://api:8000` through Service Connect without a
public endpoint or a hard-coded task address.

Survey has an explicit compute-dispatch compatibility switch in addition to
the public rollout gates:

- `SurveyRuntimeEnabled=false` keeps both Survey services at desired count zero.
- `SurveyPublicMode=off` makes `/capabilities` report Survey as unavailable and
  protects the public routes.
- `SurveyPublicMode=all` is accepted only when the runtime is enabled.
- `SurveyDispatchMode=legacy` keeps the released resident Draft/Full services
  available for rollback.
- `SurveyDispatchMode=event` sets both service desired counts to zero and
  enables the serialized control Lambda, its one-minute reconciliation rule,
  and the ECS `STOPPED` rule.

The dispatch switch is temporary Expand/Adopt compatibility. It must remain
`legacy` until migration 014, the one-shot image, Lambda IAM, checkpoint
lifecycle, and staging fault matrix have all passed. Public HTTP states remain
unchanged in either mode.

### Event execution compatibility layer

Migration `014_survey_compute_attempts.sql` is the additive Expand stage for
job-scoped Survey compute. It does not change the public Survey states or HTTP
contract, and the released long-lived workers may continue to ignore it during
an N-1 rollback. The new `scholight.survey_compute_attempts` table is the
internal record of one exact standalone task attempt. Its partial unique
indexes allow at most one `reserved`, `launching`, `launched`, or `running`
attempt per Draft or Full job.

The control plane reserves capacity before `RunTask`; the task then claims only
the Draft/job and attempt IDs carried in its command. The attempt UUID is also
the database lease owner. A second task for the same attempt, an old attempt,
or a task whose lease has expired cannot update progress or commit a
checkpoint. ECS event versions are monotonic fences: an older duplicate
`STOPPED` event cannot replace the recorded exit cause. Failure details use a
strict scalar allowlist and must never contain prompts, paper text, model
output, credentials, or user identifiers.

Full-job checkpoint pointers are nullable until the first successful commit.
Thereafter every pointer update is a compare-and-swap on both the current
sequence and `lease_owner`. `execution_deadline_at` is assigned by the first
successful exact claim and is not extended by resume attempts. OOM is retained
as the primary attempt failure class so the next attempt can select the
high-memory task definition; a later checkpoint restore error is supplemental
diagnostic data and must not replace that cause.

Event-mode Full tasks use `survey-dag-v1`, a host-owned durable DAG. RCM remains
the research-unit runtime, but it no longer owns cross-stage scheduling. Every
stage must produce and validate its declared file contract before the host can
publish a checkpoint or start a downstream stage. Reference seeds run with a
maximum concurrency of 2, paper cards with 4, and report sections with 4. Card
and section plan JSON is durable before fan-out; a restored task schedules only
units absent from the checkpoint's `completed_units` set.

The standalone reference workflows use the bounded single-seed
`reference_seed.txt` contract. The legacy expansion graph retains its separate
multi-seed `reference_expander.txt` prompt so N-1 rollback workers continue to
produce `03b_citation_expansion.md` while event mode is being adopted.

Checkpoints use
`surveys/_checkpoints/v1/{user_id}/{job_id}/objects/{sha256}` and immutable
manifests under the same job prefix. The task uploads new content-addressed
objects with `If-None-Match: *`, publishes and reads back the manifest, then
compare-and-swaps the PostgreSQL pointer using the owning attempt and expected
sequence. A new attempt also searches for exactly one valid `sequence + 1`
manifest whose parent hash and workflow/executor versions match. This closes
the crash window between S3 publication and the database CAS without replaying
the completed model unit. Multiple matching successors fail closed.

Restore always begins in an empty local workspace. It validates ownership,
path traversal, per-file and aggregate size, object key/content hash, and the
8-MiB manifest ceiling before atomically installing each file. Only durable
contracts, cards, sections, PDFs, extracts, reference shard inputs/results, and
report assets are retained. Workflow schema is restaged from the image;
trajectory and transient diagnostics are never checkpointed. The final
Markdown is checkpointed before optional PDF rendering and final archive.

The exact standalone commands are:

```text
scholight survey run-draft --draft-id <uuid> --attempt-id <uuid>
scholight survey run-job --job-id <uuid> --attempt-id <uuid>
```

They return `not-claimed` and exit successfully when the attempt is duplicated
or stale. This is expected idempotent behavior, not a worker failure.

Survey capacity has three independent limits. PostgreSQL enforces the global
and per-user limits atomically across every task; each worker separately bounds
its local process concurrency:

| Queue | Global hard limit | Per-user limit | Per-worker limit |
| --- | ---: | ---: | ---: |
| Draft | 8 | 8 | 1 per standalone task |
| Full Survey | 2 | 1 | 1 per standalone task |

The Full Survey daily quota remains 3 by default and is independent from these
simultaneous-execution limits. A quota override may permit more daily work but
never bypasses the per-user concurrency limit. The old ambiguous
`SCHOLIGHT_SURVEY_DRAFT_CONCURRENCY` and
`SCHOLIGHT_SURVEY_JOB_CONCURRENCY` names are intentionally unsupported.

Each worker makes at most three attempts for sanitized transient provider
failures such as HTTP 429, 502, 503, 504, and network timeouts. Attempts use
bounded exponential backoff while the original database lease and concurrency
slot remain owned. Authentication, resource, sandbox, and workflow-contract
failures are never retried automatically.

The vendored RCM workflows keep `http://api:8000/mcp` as their local default.
At startup the worker materializes an immutable copy of the workflow tree and
injects `SCHOLIGHT_SURVEY_MCP_URL`. Production sets this to the authenticated
same-origin `https://scholight.sanchezcloud.net/api/mcp` route, so Draft,
Discovery, and Expansion all use the same endpoint that is covered by public
health and deployment smoke tests. Bearer delegation remains mandatory; the
endpoint URL contains no credential or user data.

In ECS, a worker must establish task scale-in protection before claiming work.
It refreshes a 30-minute protection period every five minutes while work is
active and clears protection when idle. If the ECS agent endpoint is present
but protection cannot be established, the worker fails closed and does not
claim another task; local workers without `ECS_AGENT_URI` safely skip this
integration. Each worker also emits aggregate queued, running, outstanding,
oldest-queue-age, and protection-failure metrics every 30 seconds. These
metrics contain no user, Survey, topic, or document identifiers.

The Web service scales from one to three tasks at 60% average CPU or 600 ALB requests per
target per minute. The API scales from one to four tasks at 60% average CPU, 70% average
memory, or 300 requests per target per minute. Extract uses 0.5 vCPU and 1 GiB and remains at
one to three tasks on its existing CPU and memory targets. Every long-running service enables
the ECS deployment circuit breaker with automatic rollback.

Legacy-mode autoscaling uses aggregate `outstanding / running ECS tasks` metric math with
targets of 8 Drafts and 2 Full Surveys per task. Event mode does not use these
scaling policies: a Draft gets a 0.25-vCPU/0.5-GiB task, and a Full Survey gets
a 1-vCPU/4-GiB task. Only a recorded standard-profile OOM selects the
1-vCPU/8-GiB definition on the next compute attempt. Every task uses the
20-GiB Fargate default because S3 checkpoints, not local disk, are the recovery
source.
Scale-out waits 60 seconds;
scale-in waits 15 minutes so short queue gaps do not terminate expensive
workers. `SurveyDraftMaxTasks` and `SurveyFullMaxTasks` are deployment ceilings,
not steady-state counts, and both default to 1. Open capacity in the reviewed
stages `1/1 -> 2/2 -> 4/4 -> 8/8`. At `4/4`, the Full pool can run eight jobs;
at `8/8`, it reaches the global limit of 16. Before the final stage, the Fargate
On-Demand vCPU quota must be at least 64 and the model, image, and mail provider
quotas must be confirmed. Database size is governed by observed CPU, memory,
connection, and latency pressure rather than a fixed instance-class gate. The
production workflow checks AWS capacity automatically and requires an explicit
operator confirmation for external provider capacity.

The shared RDS operational alarms page when connections remain at or above 60, freeable memory
remains below 100 MiB, or swap remains above 64 MiB for 15 minutes. Those incident thresholds do
not replace the stricter Survey capacity gate below: raising a Survey worker ceiling still requires
freeable memory above 500 MiB throughout the reviewed observation window.

Each stage is an observed capacity release, not a configuration-only change.
Before raising either ceiling again, confirm that there are no OOM or abnormal
task stops, provider throttling remains below 1%, RDS CPU stays below 60%, RDS
freeable memory stays above 500 MiB, and Survey claim/heartbeat p95 latency stays
below 100 ms. The production Dashboard and alarms expose these checks without
using user or Survey identifiers. Dedicated Full Survey panels retain
service-level CPU, memory, and ephemeral-storage history for later density
reviews.

The pre-release prototype migrations, including the Survey quota-strength
change, were squashed into the single `005_survey.sql` baseline. A developer
database that applied the abandoned prototype migration chain must recreate
only the local `scholight` product schema before continuing. Do not edit or
reset shared `auth.*`, and do not run this reset against either production
region.

## Stacks

### `sanchezcloud-scholight-foundation`

Created once and updated deliberately. It owns persistent resources:

- five immutable ECR repositories;
- private, versioned Survey artifact and release-manifest buckets;
- KMS keys and aliases;
- database, application, search-provider, and Survey-provider secrets;
- the alert topic;
- GitHub OIDC image-publish, database-production, and production roles;
- the CloudFormation service role used by the runtime stack.

The protected production role may register and run only the temporary
`sanchezcloud-scholight-survey-canary` task family on the production cluster.
It can pass only the Survey execution and task roles and can read only the
Survey log group. These permissions are required for the pre-deployment model,
image, and full-text release gate; they do not permit arbitrary ECS services or
task families.

The same protected role may run the existing Scholight API task family for the
fixed, owner-preserving production Survey rerun workflow. That path can pass
only the API and shared execution roles, reads only the API log group, accepts
UUID inputs rather than an arbitrary command, and stops the task if acceptance
verification exceeds its bounded deadline. The role and workflow request a
four-hour OIDC session because a full Survey and its acceptance checks can
legitimately exceed AWS's one-hour role-session default; the workflow timeout
and ECS task deadline remain the independent upper bounds.

Persistent resources use `DeletionPolicy: Retain`. Deleting a stack is not a
cleanup operation and must never be used as a rollback mechanism.

### `sanchezcloud-scholight-production`

Updated for every application release. It owns ALB routing, task definitions,
ECS services, scheduled tasks, log groups, scaling policies, alarms, and the
dashboard. All image parameters must be digest-qualified.

The stack imports the shared platform exports produced by
`sanchezcloud-platform` and never creates another VPC or ECS cluster.

For the first stack creation only, set `ApplicationEnabled=false`. This creates
the reviewed migration task definition while all Web/API/Extract/Survey desired
counts and schedules remain zero. Run `database-production`, then redeploy the
same manifest with `ApplicationEnabled=true`. Routine releases and rollbacks
always keep it true.

The runtime template is larger than CloudFormation's inline-template limit.
The protected production workflow uploads it to the release bucket under the
short-lived `cloudformation/<release-sha>/` prefix before creating the change
set. Those upload artifacts expire after 30 days; immutable release manifests
remain under `releases/` and follow their separate retention policy.

## One-time foundation setup

Deploy the shared platform first. Then validate and create the Scholight
foundation using an administrator session:

```bash
aws cloudformation validate-template \
  --region ap-southeast-1 \
  --template-body file://deploy/ecs/scholight-foundation.yml

aws cloudformation deploy \
  --region ap-southeast-1 \
  --stack-name sanchezcloud-scholight-foundation \
  --template-file deploy/ecs/scholight-foundation.yml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    GitHubOidcProviderArn=<provider-arn> \
    AlertEmail=<operator-email>
```

Confirm the SNS email subscription. Record the stack outputs in the matching
GitHub Environment variables; do not copy secret values into GitHub.

## Secrets and database roles

CloudFormation creates secret containers, not PostgreSQL users. Before the first
deployment, an administrator must create or rotate the `scholight_app` and
`scholight_migrator` PostgreSQL role passwords to exactly match the generated
Secrets Manager values. The runtime role must not own schema objects or run DDL;
the migrator must not modify `auth.*`.

`database-bootstrap.sql` is the single reviewed role/grant contract for the new
platform. The protected compatibility and migration checks use this ECS-owned
copy; the similarly named file inside the frozen EC2 package is retained only
so that the already deployed host remains reproducible during cutover.

The migration task and the protected database workflow both pin
`SCHOLIGHT_MIGRATIONS_DIR=/app/migrations`. The API image copies the reviewed
SQL files to that path; migration execution must never depend on the Python
installation location inside `site-packages`.

Fill every required field before publishing a production release:

- `/sanchezcloud/database/scholight-runtime`: host, port, database, username,
  password;
- `/sanchezcloud/database/scholight-migrator`: the independent migrator
  credential;
- `/sanchezcloud/scholight/production/core`: independent high-entropy values for
  the application-only HMAC/JWT/internal-token fields other than MCP
  delegation;
- `/sanchezcloud/scholight/production/mcp-delegation`: the retained, generated
  MCP delegation trust anchor shared only by the Scholight issuer and Scholens
  verifier. Never copy its value into GitHub, another secret, or a local file;
- `/sanchezcloud/scholight/production/search-providers`: Zilliz, embedding,
  and MinerU endpoint/model/credential fields;
- `/sanchezcloud/scholight/production/survey-providers`: Survey model and image
- `/sanchezcloud/scholight/production/mail`: transactional email
  provider fields required by the API and the real Survey doctor. The same
  product mail credential sends Identity verification and Survey completion
  messages; it is never an Identity signing secret.

Never reuse the Identity signing secret, a database password, or an old EC2
environment value as an application HMAC secret. Never print or download a
production secret during routine deployment.

### Scholight to Scholens delegation-secret migration

The cross-product rollout order is strict because the foundation generates the
trust anchor and CloudFormation exports its exact secret and KMS-key ARNs:

1. Deploy the Scholight foundation so it creates the retained
   `/sanchezcloud/scholight/production/mcp-delegation` secret and exports
   `sanchezcloud-scholight-mcp-delegation-secret-arn` and
   `sanchezcloud-scholight-configuration-key-arn`.
2. Deploy Scholight production so the API injects
   `SCHOLIGHT_MCP_DELEGATION_JWT_SECRET` from that new secret. Wait for the old
   API task revision to drain completely. Delegation tokens are short-lived,
   so a coordinated rotation may be used during this cutover, but the secret
   value must never be manually copied between containers or secrets.
3. Deploy the Scholens foundation and runtime using those two exports. Scholens
   receives read/decrypt permission for this one trust anchor; it does not read
   the Scholight core secret.

The legacy `mcp_delegation_jwt_secret` field in the Scholight core secret is
intentionally retained during the rollout. It may be removed only after the
new Scholight task revision is stable, all old API tasks have drained, Scholens
is reading the independent secret, and a repository/account search confirms no
remaining consumer. Removing the unused field is a later reviewed rotation,
not part of the first cross-product release.

## GitHub environments

Create three protected environments:

| Environment | Required variables | Purpose |
| --- | --- | --- |
| `image-publish` | `AWS_REGION`, `AWS_PUBLISH_ROLE_ARN`, `IDENTITY_READER_APP_ID` | Build and publish immutable images and manifest |
| `database-production` | `AWS_REGION`, `AWS_DATABASE_ROLE_ARN` | Run one reviewed product migration task |
| `production` | `AWS_REGION`, `AWS_DEPLOY_ROLE_ARN`, `AWS_CLOUDFORMATION_ROLE_ARN`, `PRODUCTION_DOMAIN`, `PRODUCTION_CERTIFICATE_ARN`, `RDS_SECURITY_GROUP_ID` | Deploy or roll back an application release |

`IDENTITY_READER_PRIVATE_KEY` is the only repository dependency-reader secret.
AWS access uses OIDC; no long-lived AWS access key is stored in GitHub.

Use a required reviewer for `production` and `database-production` when the
repository billing plan supports environment reviewers. Private repositories
without that feature use the manual dispatch itself as the approval boundary:
the database workflow requires the exact phrase
`MIGRATE SCHOLIGHT PRODUCTION`, while the release workflow requires either
`DEPLOY SCHOLIGHT PRODUCTION` or `ROLLBACK SCHOLIGHT PRODUCTION`. Image
publication does not deploy and must not have database or CloudFormation
permissions.

## Release flow

```text
push main
  -> CI
  -> five immutable linux/amd64 images
  -> SBOM/provenance
  -> immutable release manifest
  -> no production change

manual database-production(release SHA)
  -> verify manifest
  -> clone reviewed migration task definition
  -> replace only the API image digest
  -> run one Fargate migration task
  -> inspect exit code

manual production(release SHA, Survey dispatch mode)
  -> verify manifest
  -> defer a changed Survey image while Draft or Full leases are active
  -> verify final-stage AWS and provider capacity
  -> deploy digest-qualified runtime stack
  -> wait for every ECS service
  -> external smoke tests
```

Application rollback selects an older manifest SHA. It does not move an image
tag, rebuild code, restore a database snapshot, or reverse an additive schema
migration.

For Survey compute rollback, set `SurveyDispatchMode=legacy`. This disables
both EventBridge rules and returns the two ECS services to desired count one.
Do not delete migration 014, attempt rows, or checkpoint objects. Already
running standalone tasks are fenced by their attempt lease and may finish; a
resident worker cannot claim the same work while that lease remains valid.

For event-mode adoption, first wait until legacy Draft and Full leases are
empty, then deploy the same digest with `SurveyDispatchMode=event`. Verify the
Lambda DLQ is empty, queued-to-RUNNING p95 is at most 120 seconds, and both
Survey ECS services stay at zero desired/running tasks. A non-empty control DLQ
pages immediately. API wakeup is asynchronous and best effort; the one-minute
rule is the durable latency bound when invocation fails.

The production workflow reads only aggregate CloudWatch activity metrics before
replacing a changed Survey worker image. Any active Draft or Full lease keeps
the current Survey digest while the API, Web, and other safe services continue
their release. Re-run the same release after the queue becomes idle to apply the
desired Survey digest. Missing metrics fail closed; after directly confirming that
both database queues have no active lease, the operator may enter the exact
one-time phrase `SURVEY WORKERS IDLE`. This confirmation cannot override a
positive activity metric. The workflow records any deferred digest in its job
summary so the partial worker rollout cannot be mistaken for a complete one.
Task CPU or memory changes are stricter: both activity metrics must be present
and report zero running work, otherwise the release fails before CloudFormation.
The Full Survey release canaries run with the candidate task CPU and memory.

## First cutover

1. Resolve the local/remote main history and publish one reviewed release
   candidate.
2. Deploy the shared platform and foundation stacks with Survey gates off.
3. Complete container, PostgreSQL, MinIO, Playwright, fake Survey, and real
   Survey-doctor tests.
4. Promote the fully caught-up Singapore RDS replica only after the Hong Kong
   writers are paused and replication lag is zero.
5. Create least-privilege runtime/migrator roles and fill Secrets Manager.
6. Point the existing legacy Scholight EC2 runtime at Singapore RDS without
   releasing new application code. This preserves an application rollback path.
7. Deploy the production stack once with `ApplicationEnabled=false`, Survey off,
   and schedules disabled. This creates the reviewed migration task definition
   without starting an application service.
8. Run the protected product migration exactly once against that dormant stack.
9. Deploy the same release manifest with `ApplicationEnabled=true`, while Survey
   remains off and schedules remain disabled.
10. Smoke-test Search, authentication, history, Access Keys, MCP, Extract, queue
   drain, and internal Survey on the candidate hostname.
11. Move Cloudflare to the ALB and enable schedules.
12. Enable Survey runtime while public mode remains `off`; pass the real doctor,
    artifact, email, cancellation, and recovery checks.
13. Change Survey public mode directly from `off` to `all`. There is no legacy
    mode to preserve.
14. Observe Search for 24 hours and retain the old EC2 and Hong Kong database
    rollback references for seven days before requesting cleanup.

Survey runtime logs are retained in `/sanchezcloud/scholight/survey`. Image tool
events retain only a stable error code, HTTP status, retryability, and duration;
prompts, credentials, and provider response bodies are never archived. The
production dashboard separates image successes and failures, and alerts when at
least three image calls fail with no success in a six-hour window. Any finalizer
failure alerts immediately because it means paid research completed without a
deliverable report.

RCM completion failures are likewise content-free. The pinned RCM 0.2.21 emits
the completion outcome, HTTP status, stable failure kind, retryability, and
elapsed time. Scholight accepts its additive
request-shape contract: serialized request bytes, estimated tokens, message and
tool counts, thinking/reasoning shape, unmatched or duplicate tool-call counts,
sanitized provider identifiers, and `request_size`, `thinking_tool_history`, or
`unknown_request`. Arbitrary event fields are discarded, and the legacy `taken`
hitch shape remains a rolling-upgrade facade whose text is never archived.
Terminal model failures and full-text runtime failures have one-event alarms.
The Dashboard shows their stable codes, full/partial/abstract evidence counts,
and aggregate full-text coverage without paper, topic, user, or Survey
dimensions.

A zero-exit RCM run may retain a classified model failure from the optional
`image_planner` component. The Survey worker still runs its deterministic evidence
audit and local finalizer when the required outline, sections, and cards are
complete. Failures from required or unknown components remain terminal. An
optional image-path failure alone must not discard otherwise complete research;
it also cannot mask an earlier required-component failure. If local checks cannot
produce a valid report, the model failure remains terminal.

RCM 0.2.21 preserves provider reasoning and reconstructs visible assistant text
plus its tool calls as one assistant turn; call-correlated failed tool results
are replayed as valid outcomes without re-splitting the turn. This is required
by DeepSeek thinking mode when that mixed turn is replayed with its original
`reasoning_content`.

RCM 0.2.21 additionally replaces HTTP provider response bodies on the machine
tape with a status-only hitch and publishes the content-free request class on
`completion_end`. The standalone DAG treats a failed terminal completion as a
unit failure even when the RCM process exits zero. A reference seed classified
as `request_size` is halved and retried once; `thinking_tool_history` switches
once to the canaried non-thinking workflow; `unknown_request` is not replayed.
The latest sanitized failure is retained on the exact compute attempt and a
clean ECS exit cannot erase it.

The Survey image pins the 0.2.21 Linux archive from the primary RCM repository
with its published SHA-256. A source commit, local binary, or guessed checksum
is not a release artifact. The production deployment must still pass the fixed
model, image, and full-text canaries against the candidate Survey image before
the pinned runtime reaches the service.

Run the fixed provider canary from a one-off task cloned from the Survey task
definition; it bypasses model completion and never prints its prompt, key, or
response body:

```bash
scholight survey image-canary --json-output
```

The output contains only `error_code`, HTTP status, retryability, elapsed time,
and the provider's sanitized code/reason classification. Raw response text is
never returned. Use `ImageGenApiUrl` only for a reviewed gateway override. If
successful URL responses use another public HTTPS host, add its exact hostname
to `ImageGenTrustedHosts`; private addresses, redirects, MIME/signature
mismatches, and files above 20 MiB remain rejected.
Listing the configured image model through a general `/v1/models` endpoint is
not an image authorization check: an image canary that returns HTTP 401 or 403
blocks the release until the provider grants image-generation access to the
configured credential. A successful, signature-validated canary is required
before deploying a new RCM pin to production.

Run the fixed full-text canary from the same candidate Survey task. It downloads
one fixed public PDF, verifies the PDF signature, executes `pdftotext`, and
requires a minimum extracted character count:

```bash
scholight survey fulltext-canary --json-output
```

The result contains only status, a stable error code, duration, and character
count; it never returns paper text. Both image and full-text canaries must pass,
along with the fixed DeepSeek protocol canary, before the Survey image may be released:

```bash
scholight survey model-canary --json-output
```

The model canary uses the exact production OpenAI-compatible model declaration,
including explicit thinking-mode selection and history compatibility. It reads one fixed non-sensitive
file, then must emit visible text and an `fs` write in the same assistant turn
before completing a third model turn. The command requires three successful
completions, two complete tool round trips, a mixed response with at least two
fragments, and the exact fixed output file. This catches providers that reject a
missing `reasoning_content` field when visible text precedes a tool call. It
discards completion text and provider response bodies, retaining only status,
error code, HTTP status, retryability, duration, the sanitized provider/request
classification, and content-free request-shape counters. The canary output uses
the same strict allowlist as Survey attempt diagnostics.

The immutable release workflow enforces this contract before CloudFormation
changes the running services. It clones the deployed Survey task definition,
replaces only its image with the digest-qualified candidate, runs all three
fixed canaries in the private production network, checks the container exit
code, and deregisters the one-off task definition. A failed canary therefore
stops the release before the production Survey service is updated.

Owner-preserving production reruns use the separately confirmed
`Run production Survey rerun` workflow. It accepts only a terminal source Survey
UUID and a stable operation UUID, verifies the exact deployed API digest, derives
deterministic new Survey, Draft, and Job identifiers, and executes the fixed
`production_ops` module. The operation copies the source owner and initial
request without printing either, preserves the old record, waits for terminal
success, verifies the archive, report, and package hashes, requires at least 80%
full, partial, or HTML evidence coverage, rejects runtime metadata leakage, and
requires exactly one successful completion-notification outbox record.
Re-dispatching the same operation UUID is idempotent and cannot create or charge
a second Survey.

One archived degraded evidence incident is repaired through the separately
confirmed `Repair production Survey evidence` workflow. The workflow is fixed to
Job `f4795522-28f6-4edd-8813-102f654d4367`; neither a Job UUID nor a command can
be supplied at dispatch time. It verifies the exact deployed API and Survey
images against the immutable release manifest, then runs the read-only
`repair-degraded-evidence` verification in an owner-scoped Survey Fargate task.
An `apply` dispatch performs that same verification first and proceeds only when
the immutable source manifest and report still match their reviewed SHA-256
guards. Apply repairs only cards selected from `00_card_plan.json`, does not run
image generation, and never replays retrieval, outlining, or the complete Survey
graph. The workflow shares the `scholight-production` concurrency lock with
deployment and deregisters its one-off task definition on completion.

The 2026-08-17 production canary succeeded after the image-route credential was
updated. It returned a signature-validated PNG through the configured
`gpt-image-2` route. Continue to use the real canary, rather than model listing,
as the release and incident-resolution check.

Survey artifact readers accept the original manifest v1 and additive manifest
v2/v3 recovery overlays. A v2 manifest must live below the same
owner-scoped job prefix, reference the exact v1 manifest and its SHA-256, and
may replace only `run/08_survey.md` and `run/index.md`. Downloads merge those two
records over the immutable v1 file set. Report and diagnostic reads verify the
selected object's size and SHA-256 before returning it; deletion validates and
removes both layers. This reader compatibility must be deployed and retained as
the stable rollback release before any recovery command is allowed to write v2
manifests.

Manifest v3 is the append-only evidence-repair overlay. It also references the
exact immutable v1 parent, may replace between one and 100 existing
`run/cards/*.md` records plus the mandatory regenerated `run/08_survey.md` and
`run/index.md`, and rejects all other paths, symlinks, oversized objects, parent
overlays, and nested overlay chains. Its recovery directory is content-addressed
from the parent SHA-256 and every replacement path/digest, so retries are
idempotent and conflicting writes fail closed. The database manifest pointer is
switched only after the overlay and merged workspace pass checksum and evidence
validation. Rollback is a guarded database-pointer change back to the v1
manifest together with restoring `survey_quality_degraded`; the immutable v1
objects are never overwritten. Delete and cleanup validate both layers before
removing exactly their manifest-authorized objects.

## Failure handling

Paper cards must declare one evidence level and its matching stable reason:
`html/html_text_extracted`, `full_text/pdf_text_extracted`,
`partial/pdf_text_truncated`, or `abstract_only` with a genuine scan, download,
empty-text, or extraction failure. PDF reads page through bounded output until
EOF; hitting the extraction cap is `partial`, not `full_text`. Missing or
inconsistent declarations first trigger one bounded `card_repair.rcm` pass for
at most 100 cards mapped back to immutable `00_card_plan.json`. The worker then
repeats evidence audit and deterministic finalization. A clean result publishes
normally and consumes the future Survey allowance; a failed mapping, repair, or
re-audit leaves the readable report free with `survey_quality_degraded`. Other
quality warnings do not trigger this repair. Runtime markers and coverage below
80% also remain free degraded deliveries rather than hiding the report. The
worker still refuses to claim new jobs when `pdftotext` itself is absent. Reports
expose one aggregate evidence-coverage paragraph and must not reproduce
workflow, PaperCard, or runtime metadata in reader-facing prose.

Publication and research quality are separate contracts. A non-empty, regular,
owner-scoped UTF-8 `08_survey.md` is delivered as a successful report after the
normal archive hash checks. Missing intermediate artifacts, incomplete Judge
fields, model steps that failed after sufficient materials were produced, and
evidence-quality warnings set `survey_quality_degraded`, release the quota
reservation, and show a reader-facing quality note. Repeated identical
normalized Judge verdicts are accepted. Only an absent or unreadable report,
unsafe paths, archive-integrity failures, or reader-facing internal workflow
metadata remain publication failures. `SurveyPublicationCount{outcome}` records
`succeeded`, `degraded`, `failed`, or `cancelled` independently of execution
diagnostics.

Migration `013_allow_free_readable_surveys.sql` broadens the existing terminal
quota constraint so `succeeded + released` represents a readable degraded
report that did not consume the daily allowance. It introduces no new status or
quota-state value, so the immediately previous application remains able to read
and delete these rows after rollback; older code simply never creates them.

Model failures take precedence over a secondary missing-report symptom. HTTP
429, 408, 425, 5xx, network, and timeout failures use the existing maximum of
three clean-workspace attempts only while no readable report or deterministic
finalization input set exists. If local materials can produce a readable report,
the report is delivered without charge instead of replaying the research graph.
Authentication, other 4xx responses, configuration faults, and deterministic
finalizer errors never replay the complete research graph.

Runtime artifact repair never replays the complete research graph. Missing
reports may use validated `00_card_plan.json` and `00_sections.json` to target
only absent outputs before deterministic finalization. A readable report with
invalid evidence declarations may schedule exactly one targeted PaperCard
repair; other contract and evidence diagnostics affect only quality
classification and quota settlement. Image output is never a repair,
publication, or quality-degradation condition.

The historical evidence repair has a separate acceptance contract from a new
Survey rerun. The repaired Survey and Job must be succeeded with no quality
error, every PaperCard declaration must use a valid level/reason pair, no runtime
marker may remain, and evidence coverage must be at least 80%. Its quota state
must remain `released` (a new clean rerun must instead be `consumed`), the Survey
ID and URL do not change, and the existing notification row is not inserted,
reset, or retried. The database transition has no quota-ledger or notification
write and is idempotent when the same verified v3 manifest is already active.

Component-finish artifact observations are provisional because a streamed
completion event can precede the final filesystem flush. The final contract
audit removes only an earlier missing-artifact or invalid-plan anomaly that the
final validated filesystem state disproves. Unresolved anomalies remain visible
in diagnostics; they make a readable report free instead of making it
unavailable.
Section-plan card references must use canonical ids from `00_card_plan.json`.
For legacy slash ids, diagnostics may normalize an artifact stem such as
`math-0208020` back to `math/0208020` only when the validated card plan provides
one unique match. No arbitrary or ambiguous alias is accepted.

An archived Full Survey that failed only with `survey_contract_violation` may be
reclassified in place after the corrected application proves the complete
archive can still produce the exact immutable final report and index. Historical
plan and Judge intermediates are interpreted under the schema that produced the
archive rather than retroactively rejected by newer workflow-only contracts;
new runs still receive the current strict contract audit. Run the command inside
a one-off task cloned from the deployed Survey task definition so it uses the
reviewed database role, private artifact bucket, and exact release image. The
command is dry-run by default:

```bash
scholight survey recover-archived <job-uuid> --json-output
```

Record the reported `source_manifest_sha256`, `report_sha256`, recovery type,
and expected manifest, review the zero-error result, then apply both immutable
archive guards:

```bash
scholight survey recover-archived <job-uuid> \
  --apply \
  --expected-source-manifest-sha256 <dry-run-source-manifest-sha256> \
  --expected-report-sha256 <dry-run-report-sha256> \
  --json-output
```

Recovery accepts a finished `survey_contract_violation` only when deterministic
finalization reproduces the exact archived report and index hashes. A missing
report finalization failure additionally requires complete validated card and
section plans; its newly assembled `08_survey.md` and `index.md` are written as
an append-only manifest v2 overlay referencing the exact v1 source hash. The
normal runtime card-plan limit remains 100. Recovery alone accepts at most 256
historical card-plan entries so early archives that legitimately completed more
cards can be verified without weakening the current workflow limit; every
planned card must still be present and pass the same path and content checks.
Recovery also accepts an archived absolute `run_dir` value because it never
dereferences that stale container path: expected card and section paths are
derived again from validated IDs, numbers, and slugs inside the owner-scoped
restored workspace. Live execution and repair still require the current run
directory. The database update validates the original manifest, status, error, ownership, and
replacement prefix while locking the quota ledger, Survey, job, drafts, and
notification in canonical order. It then switches the manifest pointer,
consumes quota once, and resets the completion notification to a zero-attempt
success delivery. A running notification aborts the operation; repeating an
already applied recovery neither consumes quota nor resends mail.

- A failed image build produces no deployable manifest.
- A failed database task leaves production application images unchanged.
- A failed change set rolls back the runtime stack; inspect stack events before
  retrying.
- A failed smoke test is a failed deployment even if CloudFormation stabilized;
  select the preceding manifest using the same production workflow.
- Never delete or overwrite a release manifest or ECR digest during incident
  response.
- Never enable Survey publicly to work around a failed internal doctor.
