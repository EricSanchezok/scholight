# Collection archives and recovery

This release prepares tools only. It does not export company data, write personal
S3 objects, import a personal collection, or delete any data or cluster.

The later migration recipe is **both company collections → complete personal S3
archives → only `arxiv_papers` in personal Zilliz**. Source deletion is a separate
operation after independent acceptance; none of these commands delete a source.

## Install and identify the endpoints

Use `uv sync --frozen --extra archive`. Inject `SCHOLIGHT_ZILLIZ_URI` and
`SCHOLIGHT_ZILLIZ_TOKEN` through the credential mechanism approved for the migration.
Set `SCHOLIGHT_DISABLE_DOTENV=1`; every remote command also requires `--expect-uri`
matching the injected endpoint. There is no default company endpoint or fallback.
Use the AWS provider chain for S3. Credentials are never part of a manifest.

Record the actual embedding model in `SCHOLIGHT_EMBEDDING_MODEL`; the dimension
comes from the source schema. Keep the exact code revision and lockfile alongside
the operator record. The manifest includes installed package versions and a hash
of the application Python sources. No embedding service is called by archive tools.

## Export and verify

```sh
scholight store export --expect-uri "$SOURCE_URI" \
  --collection arxiv_papers --collection arxiv_chunks \
  --destination s3://YOUR-ARCHIVE-BUCKET/APPROVED-RUN --source-frozen
scholight store verify s3://YOUR-ARCHIVE-BUCKET/APPROVED-RUN/arxiv_papers
scholight store verify s3://YOUR-ARCHIVE-BUCKET/APPROVED-RUN/arxiv_chunks
```

Replace the dedicated S3 prefix with a local directory for local storage. Each
collection has its own manifest. Collections are always explicit, independent of
the application runtime profile; exporting chunks does not turn on its workers.

`--source-frozen` asserts that **all writers have stopped before the initial
export and remain stopped throughout retries**. Counts are checked before and
after export, but stable counts alone cannot detect in-place edits. Without this
assertion, an archive remains non-final even if its files validate. Online
archives must never authorize source removal.

The official [Query Iterator](https://docs.zilliz.com.cn/docs/export-data-iterators)
provides the scan and resume checkpoint. Result order is not assumed. SDK/server
snapshot retention still limits how long an interrupted iterator can resume. If
its checkpoint expires, the command fails; start a new archive prefix while the
source remains frozen. Never edit checkpoints to bypass this failure.

## Format and resource bounds

`scholight-parquet-v1` stores Zstandard-compressed Parquet shards with:

- Original string primary key, fixed-width float32 embedding, and lossless JSON
  payload for text, arrays, scalars, and readable non-generated sparse vectors.
- Collection identity, schema, analyzer/function and index configuration, model
  and dimension, package versions, application source hash, freeze assertion,
  source/archived row counts, shard names, row counts, bytes, and SHA-256.
- A committed Query Iterator checkpoint and `complete` marker. A shard is
  uploaded and read back for checksum verification before its checkpoint commits.

Server-generated BM25 fields are excluded from row payloads and rebuilt using the
saved functions and text. User-supplied sparse fields retain their values.

The default is one 128-row read batch and one upload at a time, approximately
64 MiB of uncompressed Arrow data per shard, with at most one batch of overshoot.
No complete collection is held in memory or downloaded locally. Scratch files
live under `SCHOLIGHT_DATA_ROOT/archive-work` and are removed on exit. Downloads
are capped at 300 MiB per shard. Full primary-key uniqueness and restored-value
proofs use disk-backed SQLite with an 8 MiB cache and a 32 GiB scratch limit;
reserve disk for those proofs as well as two shard files. Hitting a limit fails
rather than silently reducing verification coverage.

S3 objects request AES256 server-side encryption. A compatible test store must
support it. Manifest writes use conditional ETags; local manifests use locked
atomic replacement. Stale concurrent metadata writers fail. Uncommitted uploaded
shards are ignored and can be reviewed separately after the archive completes.

## Restore only the selected collection

Inject **target** credentials before these commands. Run against an isolated,
empty target with all application writers stopped. Serialize recovery operators;
there must be only one archive job writing a target collection at a time.

```sh
scholight store init-archive s3://YOUR-ARCHIVE-BUCKET/APPROVED-RUN/arxiv_papers \
  --collection arxiv_papers --expect-uri "$TARGET_URI"
scholight store restore s3://YOUR-ARCHIVE-BUCKET/APPROVED-RUN/arxiv_papers \
  --collection arxiv_papers --expect-uri "$TARGET_URI"
scholight store verify-restored s3://YOUR-ARCHIVE-BUCKET/APPROVED-RUN/arxiv_papers \
  --collection arxiv_papers --expect-uri "$TARGET_URI"
```

`init-archive` verifies every source shard before creating the selected collection,
reconstructing indexes/functions, and loading it. It checks schema compatibility
and rejects a nonempty collection. It never initializes the sibling collection.
Source URI and collection identity are checked to prevent writing back into the
source, including a differently named endpoint resolving to the same collection.

Restore validates all shards, fields, dimensions, primary-key uniqueness, checksums,
and counts **before any row write**. It rejects an unrelated nonempty target.
Only a matching archive/endpoint/collection-identity recovery state can resume.
A write interrupted before checkpoint commit is replayed with identical primary
keys and vectors. Do not allow application writes until full verification passes.

`integrity_verified` means every archive row was checked. `restoration_verified`
means every target row and vector matched the archive, with no missing, extra, or
repeated keys. These are distinct proofs. A sample test proves tool compatibility;
it cannot replace these full checks on the eventual real archive.

Legacy `shard_*.jsonl.gz` files remain readable with `store restore --legacy-jsonl`
and an explicit collection/target. Invalid lines fail preflight. Their original
completeness and precision cannot be proven, so they never receive final-archive
or restored-verification status.

## Reproducible isolated evidence

```sh
docker compose -f tests/archive_integration/compose.yaml up -d --wait
SCHOLIGHT_DISABLE_DOTENV=1 SCHOLIGHT_DATA_ROOT=data uv run python tests/archive_integration/smoke.py
docker compose -f tests/archive_integration/compose.yaml down -v
```

This topology owns only loopback ports 7253, 7254, and 7290. It creates fresh
Milvus source/target and MinIO containers with synthetic credentials, tests both
collections, an interrupted upload and resumed iterator, papers-only restoration,
and full value verification. The JSON evidence is written under the data root.
