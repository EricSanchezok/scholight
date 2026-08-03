# SanchezCloud identity and Scholight data ownership

The canonical cross-product identity rules live in the
[SanchezCloud Identity engineering handbook](https://github.com/EricSanchezok/sanchezcloud-identity/blob/main/docs/README.md).
This document defines only Scholight-specific ownership and deployment constraints.

## Storage ownership

| Owner | Responsibilities | PostgreSQL ownership | Explicitly excluded |
| --- | --- | --- | --- |
| `sanchezcloud-identity` | Email identity, passwords, verification, global account status, lockout, public Account ID, shared avatar references, connected-client history, security events, audience tokens, and refresh families | `auth.users`, `auth.refresh_tokens`, `auth.user_clients`, `auth.user_avatars`, `auth.security_events`, `auth.schema_migrations` | Product roles, bans, subscriptions, quota, usage, Access Keys, search history |
| Scholight | Search access, product bans, quota, Access Keys, usage, history, ingestion state, and survey state | `scholight.*` including `scholight.schema_migrations` | Identity migrations, other product data, cross-schema writes beyond approved identity references |
| Scholight search pipeline | arXiv papers, chunks, vectors, and indexes | None; stored in Zilliz Cloud | Account or product-control data |

`auth.*` and `scholight.*` share the `sanchezcloud` PostgreSQL database but never share
ownership. Application objects must not be created in `public`. Identity is joined through the
internal `auth.users.id`; public Account IDs must not be used as relational keys.

## Sessions and browser authentication

- `client_id=scholight`, the JWT secret, audience, and `scholight_refresh` cookie are stable and
  unique to Scholight.
- Access tokens require `aud=scholight` and a current session-family `sid`.
- Browser refresh tokens use a host-only `Secure`, `HttpOnly`, `SameSite=Strict` cookie;
  browser access tokens remain in memory.
- Normal logout and session management are client-scoped. Password change/reset is a global
  Identity security event and revokes every product refresh family.
- Product bans and roles remain in `scholight.*`; only a global account disable changes
  `auth.users.status`.

## Database roles

- `auth_migrator` owns only `auth` and is used only by the protected Identity workflow.
- `scholight_migrator` owns only `scholight`, reads the Identity schema ledger, and may reference
  `auth.users` while applying product migrations.
- `scholight_app` owns nothing. It receives the minimum Identity core DML and Scholight runtime
  DML, but no migration-ledger writes, DDL, or access to another product schema.

`deploy/production/bootstrap-db.sql` is the reviewed grant contract. It runs as the database
owner before and after each independent migration so that newly created objects receive explicit
grants. It does not create login roles or store credentials.

The required order is:

1. infrastructure creates the login roles and runs the bootstrap to create owned schemas;
2. the protected Identity workflow migrates `auth.*` as `auth_migrator`;
3. the database owner reapplies grants;
4. Scholight migrates `scholight.*` as `scholight_migrator` after a schema compatibility check;
5. the database owner reapplies runtime grants;
6. CI audits `scholight_app` with the Identity `product-runtime` profile and separately verifies
   Scholight schema DML and cross-schema denials.

A Scholight deployment never carries or executes Identity migrations.

## Zilliz boundary

PostgreSQL bootstrap, Identity upgrades, and Scholight schema migrations must not drop, rebuild,
restore, or backfill the `arxiv_papers` or `arxiv_chunks` collections. Existing Zilliz data and
indexes are outside these database-role operations.
