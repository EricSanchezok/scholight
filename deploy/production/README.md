# Scholight production deployment package

This package deploys one coordinated Scholight frontend, API, metadata-sync, and
paper-ingest release to a single Docker Compose host. Caddy is the only public
service. Both ingestion services use the immutable backend digest; migrations
run explicitly before activation, and application rollback never reverses
database migrations.

## Host prerequisites

- Amazon Linux 2023 x86_64 EC2 host with its AMI-provided AWS CLI and SSM Agent
- EC2 instance role with ECR pull, SSM managed-instance, and the single Parameter Store read permission documented below
- DNS for `SCHOLIGHT_DOMAIN` pointing to the host
- inbound TCP 80/443 and outbound access to ECR, ACME, RDS, Zilliz, and the embedding API

`bootstrap.sh` installs Docker and the pinned, checksum-verified Compose v2 plugin,
creates the host directories, installs the deployment package carried inside the
backend image, and restores a missing runtime file. It does not replace an
existing `/etc/scholight/runtime.env`.

## One-time AWS and GitHub setup

Create two private ECR repositories with immutable tags, scan-on-push, and a lifecycle policy that retains the current and several previous releases:

- `scholight/backend`
- `scholight/frontend`

Configure GitHub OIDC roles instead of static AWS access keys. Repository or environment variables required by `.github/workflows/release.yml` are:

- `AWS_REGION`
- `AWS_PUBLISH_ROLE_ARN`
- `AWS_DEPLOY_ROLE_ARN`
- `ECR_BACKEND_REPOSITORY`
- `ECR_FRONTEND_REPOSITORY`
- `PRODUCTION_PLATFORM` (`linux/amd64`)
- `PRODUCTION_INSTANCE_ID`
- `PRODUCTION_DOMAIN` (for example, `scholight.example.com`)

Add the read-only `CLOUD_AUTH_READ_TOKEN` repository secret. Create a protected GitHub environment named `production` with required reviewers. The publish role may push only to the two ECR repositories; the deploy role may send and inspect SSM commands only for the production instance. The EC2 instance role needs ECR pull and SSM managed-instance permissions.

Use three distinct existing PostgreSQL login roles: `auth_migrator` owns only
`auth`, `scholight_migrator` owns only `scholight`, and `scholight_app`
receives only runtime DML. Neither migrator receives database-level `CREATE`;
each migration runner refuses a schema that is missing or owned by another role.
Never use the RDS master or `postgres` identity for the public API.

Run `bootstrap-db.sql` as the database owner before cloud-auth migration, after
cloud-auth migration, and after Scholight migration:

```bash
psql "$DATABASE_ADMIN_URL" \
  -v app_role=scholight_app \
  -v auth_migrator_role=auth_migrator \
  -v product_migrator_role=scholight_migrator \
  -f deploy/production/bootstrap-db.sql
```

The roles and passwords remain infrastructure-managed. The script never creates
login roles or stores credentials. `auth.*` is migrated only by cloud-auth's
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
`bootstrap-db.sql`, `bootstrap.sh`, `release.sh`, `smoke.sh`, and
`wait-ssm.sh`. The backend image contains those exact files under
`/opt/scholight-package`; bootstrap verifies their digest before changing the
host package. GitHub Actions never sends or mutates host runtime secrets.

## Runtime and release state

Stable secrets live only in `/etc/scholight/runtime.env`. A release manifest is secret-free and contains the production package SHA-256, a 40-character Git SHA, and backend/frontend image digest references. Host state is stored under `/var/lib/scholight`:

- `current.env` — active coordinated image pair
- `previous.env` — immediate rollback pair
- `failed/<sha>/` — diagnostics for failed candidates
- `deploy.lock` — host-side release serialization

Keep JWT and anonymous quota HMAC secrets stable across releases and rollbacks.
Parameter Store is a recovery copy: a normal release reads it only when
`/etc/scholight/runtime.env` is absent. Configuration changes remain a separate
manual change window and must update both copies before redeploying.

## Deploy

Run the manual GitHub **Release** workflow with operation `deploy`. The workflow
publishes digest-qualified images, sends only validated structured parameters to
`Scholight-BootstrapAndRelease`, waits for the terminal SSM result, and performs
an external HTTPS smoke test. Running the same release twice is supported.

The transaction converges Docker and the host package, validates runtime-file
ownership and mode, validates Compose, logs into ECR with the instance role,
pulls both images, validates the independently managed auth schema, runs only the
Scholight migration, activates the coordinated web and ingestion services, and runs bounded container and local
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
docker inspect scholight-api-1 scholight-frontend-1 \
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
