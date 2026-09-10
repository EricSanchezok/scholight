# Personal image preparation

Both existing and personal publishing workflows are **manual only**. Pull-request
and main CI build, test, and upload verification artifacts without registry pushes,
AWS credentials, releases, tags, or deployment calls.

`Publish personal images manually` accepts a revision already merged into main
and a native architecture (ARM64 by default, AMD64 supported). It first invokes
all CI gates, then uses `personal-image-publish` OIDC to publish API, Web, Extract,
and metadata images. Tags contain the resolved Git SHA and architecture; the
artifact records immutable digests. Configure ECR repositories with immutable
tags before enabling the workflow. Deploy by digest, never by a floating tag.

## Runtime targets

| Component | Image target | Runtime boundary |
| --- | --- | --- |
| Search API | `api` | Lean; no WeasyPrint, matplotlib, PDF/LaTeX tools, or browser |
| Metadata sync | `metadata` | Lean; one scheduled process, no PDF/LaTeX or Survey dependencies |
| Web | `frontend/Dockerfile` | Unprivileged Nginx, port 8080 |
| Extract | `docker/scholight-extract/Dockerfile` | Independent process and Chromium, configurable port |
| Retained full API | `api-full` | Explicit full profile and report dependencies for regression/recovery |
| Retained ingest | `ingest` | Requires explicit full profile before processing |
| Retained Survey | `survey` | AMD64 toolchain; requires explicit full profile before processing |

Native ARM64 and AMD64 CI starts the four lean images and exercises lifecycle,
search parameter compatibility, capabilities, metadata writes, and Chromium.
The retained AMD64 deployment job runs full Survey/report and ingest regressions.
WeasyPrint is upgraded to the audited version 70 series in the full profile.
The report renderer uses its URLFetcher response contract while retaining the
bundled/report-local asset allowlist and refusing external resources.
CI tooling pins pip 26.2 or newer, Vitest 4.1.11, and js-yaml 4.3.2 to
resolve audit findings; both Python and frontend dependency audits are CI gates.

Metadata commands default to one embedding request at a time. PostgreSQL advisory
locking serializes overlapping scheduled runs. A configurable 6,600-second timeout
bounds each run; SIGTERM cancels the current day without advancing its cursor.
The daily cursor and deferred ledger allow safe replay after interruption.
Apply additive migration 015 before deploying metadata. Application rollback
keeps old schemas and queue contracts intact; do not reverse the migration.

## Configuration to prepare in the migration round

No values below are created or changed by this code-preparation release.

| Location | Names / requirements |
| --- | --- |
| GitHub environment | `personal-image-publish`, limited to the approved repository/main flow |
| OIDC trust | Actual GitHub environment subject; read-only Identity app remains repository-scoped |
| GitHub variables | `AWS_REGION=ap-south-2`, `AWS_PUBLISH_ROLE_ARN` in account `669409472143` |
| Repository variables | `ECR_API_REPOSITORY`, `ECR_WEB_REPOSITORY`, `ECR_EXTRACT_REPOSITORY`, `ECR_METADATA_REPOSITORY` |
| Existing GitHub secret | `IDENTITY_READER_PRIVATE_KEY`; reuse without extracting its plaintext |
| Runtime settings | Explicit personal Zilliz URI/token, embedding provider/model, PostgreSQL role/CA, separate task roles |
| Compatibility secrets | Existing Access Key HMAC and delegated identity signing material; transfer securely during migration |
| Process isolation | Separate API, Extract, and metadata tasks; host port and memory limits assigned during deployment |
| Shared EC2 | No resource or domain changes in this round; size from measured native images before deployment |

Do not run either publishing workflow until migration prerequisites are approved.
Publishing images does not deploy services or remove old resources.
