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

Survey is a first-release capability. There is no legacy Survey mode, alias,
container entrypoint, or configuration fallback. The only controls are rollout
gates:

- `SurveyRuntimeEnabled=false` keeps both Survey services at desired count zero.
- `SurveyPublicMode=off` makes `/capabilities` report Survey as unavailable and
  protects the public routes.
- `SurveyPublicMode=all` is accepted only when the runtime is enabled.

These gates are not a compatibility layer. They let operators verify the real
RCM, artifact, queue, and email boundaries before the first public activation.

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

Fill every required field before publishing a production release:

- `/sanchezcloud/database/scholight-runtime`: host, port, database, username,
  password;
- `/sanchezcloud/database/scholight-migrator`: the independent migrator
  credential;
- `/sanchezcloud/scholight/production/core`: independent high-entropy values for
  every HMAC/JWT/internal-token field;
- `/sanchezcloud/scholight/production/search-providers`: Zilliz, embedding,
  and MinerU endpoint/model/credential fields;
- `/sanchezcloud/scholight/production/survey-providers`: model, image, and mail
  provider fields required by the API and the real Survey doctor. The same
  product mail credential sends Identity verification and Survey completion
  messages; it is never an Identity signing secret.

Never reuse the Identity signing secret, a database password, or an old EC2
environment value as an application HMAC secret. Never print or download a
production secret during routine deployment.

## GitHub environments

Create three protected environments:

| Environment | Required variables | Purpose |
| --- | --- | --- |
| `image-publish` | `AWS_REGION`, `AWS_PUBLISH_ROLE_ARN`, `IDENTITY_READER_APP_ID` | Build and publish immutable images and manifest |
| `database-production` | `AWS_REGION`, `AWS_DATABASE_ROLE_ARN` | Run one reviewed product migration task |
| `production` | `AWS_REGION`, `AWS_DEPLOY_ROLE_ARN`, `AWS_CLOUDFORMATION_ROLE_ARN`, `PRODUCTION_DOMAIN`, `PRODUCTION_CERTIFICATE_ARN`, `RDS_SECURITY_GROUP_ID` | Deploy or roll back an application release |

`IDENTITY_READER_PRIVATE_KEY` is the only repository dependency-reader secret.
AWS access uses OIDC; no long-lived AWS access key is stored in GitHub.

Production and database-production require a reviewer. Image publication does
not deploy and must not have database or CloudFormation permissions.

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

manual production(release SHA)
  -> verify manifest
  -> deploy digest-qualified runtime stack
  -> wait for every ECS service
  -> external smoke tests
```

Application rollback selects an older manifest SHA. It does not move an image
tag, rebuild code, restore a database snapshot, or reverse an additive schema
migration.

## First cutover

1. Resolve the local/remote main history and publish one reviewed release
   candidate.
2. Deploy the shared platform and foundation stacks with Survey gates off.
3. Complete container, PostgreSQL, MinIO, Playwright, fake Survey, and real
   Survey-doctor tests.
4. Promote the fully caught-up Singapore RDS replica only after the Hong Kong
   writers are paused and replication lag is zero.
5. Create least-privilege runtime/migrator roles and fill Secrets Manager.
6. Run the protected Scholight migration workflow.
7. Point the existing legacy Scholight EC2 runtime at Singapore RDS without
   releasing new application code. This preserves an application rollback path.
8. Deploy ECS once with `ApplicationEnabled=false`, Survey off, and schedules
   disabled; run the protected product migration; then deploy the same manifest
   with `ApplicationEnabled=true`.
9. Smoke-test Search, authentication, history, Access Keys, MCP, Extract, queue
   drain, and internal Survey on the candidate hostname.
10. Move Cloudflare to the ALB and enable schedules.
11. Enable Survey runtime while public mode remains `off`; pass the real doctor,
    artifact, email, cancellation, and recovery checks.
12. Change Survey public mode directly from `off` to `all`. There is no legacy
    mode to preserve.
13. Observe Search for 24 hours and retain the old EC2 and Hong Kong database
    rollback references for seven days before requesting cleanup.

## Failure handling

- A failed image build produces no deployable manifest.
- A failed database task leaves production application images unchanged.
- A failed change set rolls back the runtime stack; inspect stack events before
  retrying.
- A failed smoke test is a failed deployment even if CloudFormation stabilized;
  select the preceding manifest using the same production workflow.
- Never delete or overwrite a release manifest or ECR digest during incident
  response.
- Never enable Survey publicly to work around a failed internal doctor.
