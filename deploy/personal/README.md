# Personal PostgreSQL compatibility canary

## Production adoption

The ECS package accepts an optional public `DatabaseCaPem`. It is injected into all
eight database task definitions and the Survey control Lambda as
`SCHOLIGHT_PG_SSL_ROOT_CERT_PEM`. The database adapter validates this CA in memory,
including certificate and hostname verification; invalid PEM fails before connecting.
An empty value preserves the existing CA-file setting for the RDS deployment. Publish
and verify compatible images before setting this parameter. The earlier deployed-image
canary below remains the N-1 database-contract check.

After pausing every producer and writer, restore the final shared snapshot, then update
both runtime and migrator secrets with the destination database coordinates and their
independent role credentials. Keep the old values encrypted for rollback. The private
IP is present in the destination certificate SAN; no public database listener or DNS
workaround is needed. Resume scheduled ingestion and Survey control only after the new
connection is verified and its task-definition references have converged.

Supply `AvatarBucketName`, `AvatarRegion`, and `AvatarKeyArn` together to read the migrated
shared avatars. The API uses that bucket's explicit regional S3 endpoint and signing
region; this avoids global-endpoint redirects that invalidate presigned downloads for
opt-in regions. The API receives only object
read plus KMS decrypt permission for the avatar prefix. Account Center must authorize
the exact source API role in both destination bucket and key policies. This keeps future
avatar changes visible while Scholight compute remains in its original account. Empty
parameters preserve the original same-account reader. No search or artifact bucket moves.

Opening writes on the destination invalidates a simple connection-only rollback: first
reconcile new writes. Do not remove the source data or restore an old schema as part of
an application rollback.

## Isolated rehearsal

Scholight compute, search providers, production secrets, and its public MCP endpoint
remain in the source account during the Scholens rehearsal. Existing settings already
support a configurable database host and a verified CA (`SCHOLIGHT_PG_HOST` and
`SCHOLIGHT_PG_SSL_ROOT_CERT`); no application compatibility shim or auth migration is
introduced here. Identity continues to own `auth`, and Scholight owns only its schema.

`database-access.yml` runs in the destination account. It provisions a new encrypted
Scholight runtime credential and permits PostgreSQL only from the two reviewed source
Scholight private subnet CIDRs. Platform owns the cross-account peer and return routes.
Provision the generated password on the restored `scholight_app` role through a
controlled administrator connection, without printing it or changing the source role.
The database certificate includes the reserved private IP as a SAN, so the canary can
verify that IP without changing source production DNS.

`database-canary.yml` runs in the source account and creates no service or schedule.
Supply the exact currently deployed API image digest and the public destination CA as
base64. Its separately scoped execution role reads only a temporary destination-credential
copy, the existing API image, and its own log group. It has no AWS application task role,
no provider keys, no ingress, and egress limited to HTTPS and the destination database.
The container filesystem is disposable and writable to materialize the public CA; it
mounts no host directories or production files. Existing services are never updated.

Copy only the newly generated destination credential between Secrets Manager stores in
process memory. After reviewing both CloudFormation change sets, run the canary once
in either approved source private subnet with public IP assignment disabled. It uses
Scholight's deployed pool adapter with certificate/hostname verification, validates
Identity compatibility through its deployed user adapter, compares the administrator-read
restored migration ledger supplied as nonsecret metadata against the deployed image,
checks business-table readability and role isolation, and reports only
aggregate counts/timing. Queries run read-only; no schema migration, search request,
model call, queue consumption, or task replay occurs.

Require exit code zero and the complete success report, then retain the nonsecret
report with the rehearsal evidence. Delete the temporary source canary stack and its
credential after acceptance. Retain destination access for the later reviewed cutover.
A successful canary proves database compatibility and network reachability, not complete
Scholight product acceptance or authorization to change production connections.

Application roles intentionally cannot read either migration ledger. The operator reads
the destination ledger during preflight and supplies its version/name/checksum entries
as `ExpectedMigrationLedger`. The canary verifies those checksums against packaged SQL
and asserts that both database ledgers remain inaccessible to the runtime role. It does
not widen runtime grants to make a migrator-only schema check pass.
The task explicitly selects `/app/migrations`, matching the deployed migration task;
installed Python package paths are not the container's packaged SQL directory.
