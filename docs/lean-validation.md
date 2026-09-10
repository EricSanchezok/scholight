# Lean preparation validation record

Validation performed on 2026-09-10 using synthetic data only. This record does not
claim that company data has been archived or that personal cloud resources have
been migrated. PR and its current CI runs are the authoritative acceptance gate:
[Scholight PR #95](https://github.com/EricSanchezok/scholight/pull/95).

## Evidence

- Full local backend run including PostgreSQL: 1,392 passed, three existing skips
  (two unavailable local parser fixtures and the macOS native PDF backend).
  Subsequent CLI/archive/deployment regression checks also cover the explicit
  full-profile search guard. CI supplies the native PDF backend.
- Frontend unit tests: 135 passed. Desktop/mobile browser coverage: 32 passed,
  eight device-specific skips. Linux and macOS visual baselines cover the current
  single-search and usage surfaces; obsolete strength-menu baselines were removed.
- Native ARM64 local images started API, Web, Extract, and metadata successfully.
  GitHub also passed both native ARM64 and AMD64 image jobs. API omits report
  libraries; metadata omits PDF/LaTeX parsers. Extract launched Chromium.
- Real isolated Milvus/MinIO: 259 papers and 259 chunks, multiple shards, uploaded
  shard interruption, committed Query Iterator resume, original-vector restore,
  repeated restore, and exhaustive source-archive-target value comparison.
  Papers-only restoration did not create chunks. Both archive integrity and
  restored verification were true for each **synthetic** collection.
- Fault tests reject corrupt shards, duplicate primary keys, incomplete archives,
  invalid scalars/dimensions, wrong/source/nonempty targets, stale manifest writes,
  interrupted recovery checkpoints, and corrupt legacy JSONL before any upsert.
- PostgreSQL tests verify migration replay, durable latest-version recovery,
  dry-run, bounded enqueue, and preservation of a newer version during recovery.
  A strict stub rejects every chunks access while lean initialization, statistics,
  and health/repair checks run.

## Reproduction

Use `SCHOLIGHT_DISABLE_DOTENV=1`, the repository virtual environment, and an
explicit loopback `scholight_test_*` database as required by the test guards.
Run the full pytest suite with `-m ''`, Ruff, MyPy, Bandit, Vulture, and pip-audit.
Run frontend `npm run verify` and `npm audit --audit-level=moderate`. Linux brand
asset validation is canonical because encoded image bytes differ across platforms.

The CI artifacts `archive-roundtrip`, `browser-verification`, and
`lean-images-*` record the container/browser results for the tested commit.
Native image sizes are available in the image artifacts; disk image size is not
an application memory budget. EC2 allocation and production load measurements
remain part of the next migration round.

No registry publishing, release/tag creation, cloud deployment, DNS changes,
production writes, real archive exports, or cluster deletions were performed.
