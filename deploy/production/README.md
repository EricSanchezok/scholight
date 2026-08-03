# Scholight production deployment package

This package deploys one coordinated Scholight frontend, API, Web Extract sidecar,
metadata-sync, and paper-ingest release to a single Docker Compose host. Caddy is the only public
service. Both ingestion services use the immutable backend digest; migrations
run explicitly before activation, and application rollback never reverses
database migrations.

## Host prerequisites

- Amazon Linux 2023 x86_64 EC2 host with its AMI-provided AWS CLI and SSM Agent
- EC2 instance role with ECR pull, SSM managed-instance, and the single Parameter Store read permission documented below
- DNS for `SCHOLIGHT_DOMAIN` pointing to the host
- An optional external TLS load balancer whose origin hostname is
  `SCHOLIGHT_EDGE_DOMAIN` and whose target is HTTP port 80
- inbound TCP 80/443 and outbound access to ECR, ACME, RDS, Zilliz, and the embedding API

`bootstrap.sh` installs Docker and the pinned, checksum-verified Compose v2 plugin,
creates the host directories, installs the deployment package carried inside the
backend image, and refreshes `/etc/scholight/runtime.env` from the fixed encrypted
Parameter Store value on every deployment. The downloaded configuration is validated
before it atomically replaces the existing runtime file.

### External TLS edge origin

`SCHOLIGHT_DOMAIN` remains the canonical hostname served directly by Caddy over
HTTPS. `SCHOLIGHT_EDGE_DOMAIN` is a second, explicit hostname accepted over
plaintext HTTP only for a trusted load balancer that terminates TLS externally.
The load balancer target group must use HTTP port 80 and `/healthz` with success
code 200. That health endpoint is intentionally independent of the `Host` header;
every other request with an unknown host returns 404.

The edge load balancer must preserve the request path, `Host`, `Authorization`,
and `Content-Type` headers, must not cache `/api/mcp`, and must not automatically
retry search POST requests. Allow inbound port 80 from the load balancer security
group rather than from the whole internet. Add the edge HTTPS origin to
`SCHOLIGHT_CORS_ALLOW_ORIGINS` so browser and MCP Origin checks accept it. The
canonical `SCHOLIGHT_PUBLIC_WEB_URL` may remain on `SCHOLIGHT_DOMAIN`.

## One-time AWS and GitHub setup

Create three private ECR repositories with immutable tags, scan-on-push, and a lifecycle policy that retains the current and several previous releases:

- `scholight/backend`
- `scholight/frontend`
- `scholight/extract`

Configure GitHub OIDC roles instead of static AWS access keys. Repository or environment variables required by `.github/workflows/release.yml` are:

- `AWS_REGION`
- `AWS_PUBLISH_ROLE_ARN`
- `AWS_DEPLOY_ROLE_ARN`
- `ECR_BACKEND_REPOSITORY`
- `ECR_FRONTEND_REPOSITORY`
- `ECR_EXTRACT_REPOSITORY`
- `PRODUCTION_PLATFORM` (`linux/amd64`)
- `PRODUCTION_INSTANCE_ID`
- `PRODUCTION_DOMAIN` (for example, `scholight.example.com`)

Install the scoped Dependency Reader GitHub App on Scholight and
`sanchezcloud-identity`, set repository variable `IDENTITY_READER_APP_ID`, and store its private
key as `IDENTITY_READER_PRIVATE_KEY`. Workflows mint short-lived, contents-read tokens; do not
create a long-lived dependency PAT. Create a protected GitHub environment named `production`
with required reviewers. The publish role may push only to the two ECR repositories; the deploy
role may send and inspect SSM commands only for the production instance. The EC2 instance role
needs ECR pull and SSM managed-instance permissions.

Use three distinct existing PostgreSQL login roles: `auth_migrator` owns only
`auth`, `scholight_migrator` owns only `scholight`, and `scholight_app`
receives only runtime DML. Neither migrator receives database-level `CREATE`;
each migration runner refuses a schema that is missing or owned by another role.
Never use the RDS master or `postgres` identity for the public API.

Run `bootstrap-db.sql` as the database owner before sanchezcloud-identity migration, after
sanchezcloud-identity migration, and after Scholight migration:

```bash
psql "$DATABASE_ADMIN_URL" \
  -v app_role=scholight_app \
  -v auth_migrator_role=auth_migrator \
  -v product_migrator_role=scholight_migrator \
  -f deploy/production/bootstrap-db.sql
```

The roles and passwords remain infrastructure-managed. The script never creates
login roles or stores credentials. `auth.*` is migrated only by sanchezcloud-identity's
protected workflow. A Scholight release merely checks the installed auth schema
version and migrates `scholight.*`.

## One-time bootstrap configuration

Create a **Standard SecureString** named
`/scholight/production/runtime-env` in `ap-southeast-1`. Paste the complete
production `runtime.env` as its value, select the AWS-managed `alias/aws/ssm`
key, and confirm that the UTF-8 value is at most 4096 bytes. Use the AWS console
for this step so the value does not enter shell history, GitHub, CI logs, or this
repository.

Add only this inline permission to the existing `scholight-ec2` role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "ssm:GetParameter",
      "Resource": "arn:aws:ssm:ap-southeast-1:683390797772:parameter/scholight/production/runtime-env"
    }
  ]
}
```

Create the fixed command document from the reviewed repository file:

```bash
aws ssm create-document \
  --region ap-southeast-1 \
  --name Scholight-BootstrapAndRelease \
  --document-type Command \
  --document-format YAML \
  --content file://deploy/production/ssm-document.yaml
```

Restrict the existing GitHub deploy role's `ssm:SendCommand` statement to the
document and production instance. Keep command-result reads separate because
those APIs do not support the same resource-level restriction:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "ssm:SendCommand",
      "Resource": [
        "arn:aws:ssm:ap-southeast-1:683390797772:document/Scholight-BootstrapAndRelease",
        "arn:aws:ec2:ap-southeast-1:683390797772:instance/REPLACE_WITH_PRODUCTION_INSTANCE_ID"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "ssm:GetCommandInvocation",
        "ssm:ListCommandInvocations",
        "ssm:CancelCommand"
      ],
      "Resource": "*"
    }
  ]
}
```

Remove any remaining permission to send `AWS-RunShellScript`. No new IAM role,
S3 bucket, Secrets Manager secret, or custom KMS key is required.

Each release carries a SHA-256 digest of `compose.yaml`, `Caddyfile`,
`cloudwatch-agent.json`, `bootstrap-db.sql`, `bootstrap.sh`, `compose-command.sh`,
`release.sh`, `smoke.sh`, and `wait-ssm.sh`. The backend image contains those exact files under
`/opt/scholight-package`; bootstrap verifies their digest before changing the
host package. GitHub Actions never sends or mutates host runtime secrets.

## Survey activation and artifact permissions

`SCHOLIGHT_SURVEY_ENABLED` must be present in `runtime.env` and must be exactly
`true` or `false`. The reviewed `compose-command.sh` is the single Compose entry
point used by deploy, rollback, smoke, and diagnostics; it adds the `survey`
profile only when the setting is `true`. A compatibility release therefore keeps
both Survey workers absent without requiring Survey provider credentials.

When Survey is activated, the existing EC2 role may access only the dedicated
Survey bucket. Object permissions remain limited to `surveys/v1/*`. Prefix listing
is used only when a server-generated manifest is missing during cleanup:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::scholight-surveys-683390797772-ap-southeast-1/surveys/v1/*"
    },
    {
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::scholight-surveys-683390797772-ap-southeast-1",
      "Condition": {
        "StringLike": {"s3:prefix": "surveys/v1/*"}
      }
    }
  ]
}
```

Do not grant access to any other bucket or prefix. Updating this policy and the
SecureString does not itself activate Survey; activation also requires the
reviewed release after the EC2 resize and local E2E gate.

Each execution archives a private `run/trajectory.jsonl`, `run/diagnostics.json`,
and schema-v2 `run.json` under the existing owner-scoped Survey prefix. These
files contain bounded, redacted runtime metadata; they never contain provider
credentials, PDF bodies, or model reasoning. Diagnose an active or archived job
from the worker container without rerunning it:

```bash
./deploy/production/compose-command.sh exec -T survey-worker \
  /app/.venv/bin/scholight survey diagnose JOB_UUID --json-output
./deploy/production/compose-command.sh exec -T survey-worker \
  /app/.venv/bin/scholight survey contract-audit --json-output
./deploy/production/compose-command.sh exec -T survey-worker \
  /app/.venv/bin/scholight survey status --json-output
```

CloudWatch service logs carry the same `survey_job_id` across the worker and its
delegated MCP searches. Search queries remain only in the private per-run trace,
not in centralized logs or metric dimensions.

Survey completion email is opt-in per run. The terminal Survey update and its
notification outbox row commit in one database transaction after report archival;
failed runs are also eligible, while user cancellation is not. The existing
Survey worker claims notifications independently, retries temporary DirectMail
failures up to eight times, and never changes the Survey result when email delivery
fails. It resolves the account's current verified email only when sending. Configure
the existing `SCHOLIGHT_ALIYUN_DM_*` values and the canonical
`SCHOLIGHT_PUBLIC_WEB_URL` before enabling Survey. `scholight survey status` reports
pending, retrying, sent, and dead notification counts without exposing addresses.

## One-time observability stack

After the P0 runtime release has completed its observation window, deploy the
reviewed CloudFormation template. It creates only the two 14-day log groups,
minimal write-only instance policy, dashboard, SNS email subscription, metric
filters, and alarms described in the template:

```bash
aws cloudformation deploy \
  --region ap-southeast-1 \
  --stack-name scholight-production-observability \
  --template-file deploy/production/observability.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    InstanceId=REPLACE_WITH_PRODUCTION_INSTANCE_ID \
    AlertEmail=REPLACE_WITH_OPERATIONS_EMAIL \
    InstanceRoleName=scholight-ec2
```

Confirm the SNS subscription from the operations mailbox before relying on
notifications. Bootstrap installs and starts rsyslog and the CloudWatch Agent
idempotently from the package configuration. The policy cannot read Parameter
Store, access RDS or Zilliz, or modify application data.

The dashboard includes Survey outcome, duration, contract, tool, runtime,
last-activity, and email-delivery panels. Contract violations, runtime failures,
diagnostic write failures, dead email notifications, email backlog older than 15
minutes, and two consecutive periods above 30 minutes without Survey activity raise
alerts; cancellation does not.

## Runtime and release state

Stable secrets live only in `/etc/scholight/runtime.env`. A release manifest is secret-free and contains the production package SHA-256, a 40-character Git SHA, and backend/frontend image digest references. Host state is stored under `/var/lib/scholight`:

- `current.env` — active coordinated image pair
- `previous.env` — immediate rollback pair
- `failed/<sha>/` — diagnostics for failed candidates
- `deploy.lock` — host-side release serialization

Keep JWT and anonymous quota HMAC secrets stable across releases and rollbacks.
Parameter Store is the source of truth for stable runtime configuration. Every normal
deployment downloads, validates, and atomically installs it before starting the release.
Update the SecureString in a separate configuration change window before redeploying.

## Deploy

Run the manual GitHub **Release** workflow with operation `deploy`. The workflow
publishes digest-qualified images, sends only validated structured parameters to
`Scholight-BootstrapAndRelease`, waits for the terminal SSM result, and performs
an external HTTPS smoke test. Running the same release twice is supported.

The transaction converges Docker and the host package, validates runtime-file
ownership and mode, validates Compose, logs into ECR with the instance role,
pulls all three images, validates the independently managed auth schema, runs only the
Scholight migration, activates the coordinated web, extraction, and ingestion services, and runs bounded container and local
TLS-ingress smoke checks. Pull, package, configuration, or migration failure
leaves the running application untouched. Candidate host-smoke failure restores
the complete previous pair.

## Roll back the application

```bash
sudo /opt/scholight/release.sh rollback
```

Rollback validates and pulls both previous digest-qualified images before switching the coordinated pair and running the same smoke checks. It does not run down migrations. Normal release migrations must therefore follow expand/contract compatibility with the immediately previous application.

## Diagnostics

```bash
sudo /opt/scholight/release.sh status
docker compose --env-file /etc/scholight/runtime.env \
  --env-file /var/lib/scholight/current.env \
  -f /opt/scholight/compose.yaml ps
```

Public `/api/livez` and `/api/readyz` deliberately return 404. Readiness is checked from inside the API container. A real search is not used for smoke testing because it consumes quota and depends on a side-effecting search path.

### Interrupted transition reconciliation

`transition.env` is a fail-closed crash journal. A deploy or rollback interrupted after activation starts blocks every later release operation. Never delete `transition.env` before verifying the running image pair against the referenced manifests and completing one row of the matrix below. Reconcile only when no `release.sh` process is running.

First record the journal and running state:

```bash
sudo /opt/scholight/release.sh status || true
sudo cat /var/lib/scholight/transition.env
docker inspect scholight-api-1 scholight-frontend-1 scholight-extract-1 \
  --format '{{.Name}} {{.Config.Image}} {{.Image}}'
```

Use `SCHOLIGHT_TRANSITION_TARGET` as `TARGET_ENV`. Validate that it is a regular manifest whose backend/frontend digest references match the running containers before completing a promotion. Then follow exactly one case:

| Journal state | Supported reconciliation |
|---|---|
| `deploy / activating` | Treat the candidate as untrusted. If `current.env` exists, re-run Compose `up -d --no-build --remove-orphans` with `current.env` and run `smoke.sh`; on a first deployment, bring the candidate down. Preserve the candidate under `failed/<sha>/` when available, then remove only `transition.env`. |
| `deploy / activated` | Activation smoke passed but manifest promotion may not have finished. If `TARGET_ENV` exists and its digests match the running pair, atomically move the old `current.env` to `previous.env`, move `TARGET_ENV` to `current.env`, re-run `smoke.sh` using `current.env`, then remove `transition.env`. If any check fails, use the `deploy / activating` recovery path instead. |
| `rollback / activating` | Treat the rollback target as untrusted. Restore `rollback-current.env` with Compose, run `smoke.sh`, keep the existing `previous.env`, remove `rollback-current.env`, then remove `transition.env`. |
| `rollback / activated` | The previous pair passed smoke but the manifest swap may not have finished. Confirm its digests match the running pair, move `previous.env` to `current.env`, move `rollback-current.env` to `previous.env`, re-run `smoke.sh` using `current.env`, then remove `transition.env`. If checks fail, restore `rollback-current.env` as in `rollback / activating`. |

For every Compose command above, use both `--env-file /etc/scholight/runtime.env` and the selected release manifest with `/opt/scholight/compose.yaml`. Invoke smoke as `SCHOLIGHT_RELEASE_ENV=<selected-manifest> /opt/scholight/smoke.sh`. If a referenced manifest is missing or image identity is ambiguous, stop and reconstruct the manifest from ECR and release records rather than guessing or clearing the journal.

## Package upgrades

The release contract version is `1`. Do not edit the production package
independently. A deploy always extracts the package from the reviewed backend
image and installs it only after the package SHA matches the selected commit.
