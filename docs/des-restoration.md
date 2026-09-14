# Destination-bound ingestion and des restoration

## Database expansion

Migration 016 adds destination identity, target queues, selected full-text scope,
verified completion receipts and resumable install records in `scholight` only.
It does not alter legacy jobs, shared identity, existing migration checksums, or
legacy synchronization cursors. N-1 application reads remain valid. Migration
execution must precede adopting workers; application rollback has no down migration.

A destination key binds the endpoint, actual papers/chunks collection IDs,
embedding model and dimension. Identical collection names in another cluster do
not share receipts, jobs or progress. A verified reconciliation manifest establishes
the new `arxiv:<destination-key>` daily baseline; workers cannot infer it from a
legacy success date. Baseline rebinding to another manifest is rejected.

## Queue and completion contracts

New/revised papers take priority. Every fifth successful claim prioritizes a
backfill job waiting longer than one day. The counter is durable and serialized
per destination, including across controller or worker restarts. Each claim has a
new lease token. Expired or superseded leases cannot renew, release, or finish work.
A newer paper version fences its previous lease; replaying an older version cannot
downgrade it. Identical replay does not duplicate work.

Successful completion requires a receipt for the actual destination, exact paper
version and current processing configuration. Receipts retain chunk count,
checksum, processing configuration and durable recovery-manifest reference.
`has_chunks` and succeeded jobs in the legacy queue are not such evidence.

The new queue is initially an additive implementation. Production adoption also
requires the bounded worker/install integration, reconciliation verification and
reviewed five-component release. Until then existing production schedules remain
unchanged. Legacy query functions are the N-1 compatibility boundary; retire their
worker use only after old worker revisions are no longer rollback candidates.

## Scope and operational milestones

Recovery covers only new/revised papers missed during lean, paused or migration
periods. Combine the deferred ledger, version-aware abstract differences and
relevant failed tasks; preserve the audit source and latest requested version.
Do not widen recovery to all historical `has_chunks=false` records.

Production online and selected historical recovery complete are distinct results.
An empty runnable queue does not prove success: report dead, inaccessible and
withdrawn papers separately. Do not delete the old personal collection or alter
unrelated des collections. Survey stays disabled.

## Daily coverage and replay

Full ingestion re-registers every trusted observed version, including an unchanged
v1 after a vector-write/PostgreSQL-registration interruption. A missing exact
source version fails the full-mode day. Metadata and task registration must finish
before the date cursor advances; full-text completion may remain queued.

Only completed OAI pagination proves new-and-revised coverage. Empty HTTP bodies,
malformed XML, unparsable active records and repeated tokens fail the harvest.
Authoritative OAI `noRecordsMatch` is an empty completed day. Atom submission-date
fallback can preserve newly fetched metadata but leaves the date retryable because
it does not cover all revisions. XML parsing uses the declared defusedxml dependency.

## Destination adoption

`SCHOLIGHT_INGESTION_TARGET_ID` selects the destination query adapter. Metadata
and ingestion commands verify both actual collection IDs, endpoint, embedding
model and dimension, then require its verified baseline before doing work.
With this binding set, queue queries never read or update legacy jobs. The old
SQL path remains an N-1 adapter until all full-ingestion consumers adopt target
baselines; its retirement requires a reviewed contract-removal change.

Daily registration records the target's fulltext scope before advancing its
independent cursor. Broad missing-chunks reconciliation is disabled for bound
targets. `resume-fulltext` selects only their reviewed scope, retains audit rows
and leaves terminal failures for explicit review. It does not retry dead jobs
implicitly. Each claim's unique lease owner is propagated through heartbeats,
release and completion so an expired worker cannot finish a reclaimed job.

## Fulltext installation and interruption recovery

Bound workers retain exact-version LaTeX/PDF behavior and split embedding calls
into sequential batches of at most 64 chunks. Prepared float32 vectors and old
chunks are stored in checksummed Parquet/Zstandard shards under the dedicated
`SCHOLIGHT_INGEST_RECOVERY_URI`. A manifest commits only after shard upload and
readback verification. New version/configuration primary keys cannot overwrite
old chunks while preparation is incomplete. No corpus-wide vector scan occurs.

Every installation holds a per-paper PostgreSQL advisory lock and checks the
current unique lease and paper version between stages. It writes all new chunks,
verifies every scalar/vector value, then deletes only the old keys listed in the
recovery manifest. Completion receipts bind target, version, chunking settings,
embedding model/dimension, chunk count and checksum. A retry resumes the saved
manifest without generating embeddings again. A changed manifest is rejected
against its database checksum. SIGTERM, deadline cancellation and lost leases
stop at bounded operation boundaries; cancellation joins the active write before
releasing the paper lock or deleting temporary files.

The drain command defaults to a 30-minute window. Broad `enqueue-backfill` is
unavailable with an adopted destination; use the reviewed scope instead. Recovery
prefixes require 30-day retention configured by the infrastructure rollout.
The isolated integration suite pins Milvus 2.6.23 because merge-mode upsert
requires [Milvus 2.6.2 or newer](https://milvus.io/docs/v2.6.x/upsert-entities.md).
It verifies interruption and exact replacement against real Milvus, PostgreSQL
and MinIO in addition to fault-injection tests.

## Scalar inventory and abstract delta planning

`scan_inventory` captures only paper IDs, versions and creation/update dates with
Query Iterator. Reads remain limited to 1,024 scalar records; up to 16,384 records
are coalesced into each compressed Parquet shard to reduce S3 round trips. A shard
and its last included iterator checkpoint are committed only after durable upload
and checksum readback. Interrupted, uncommitted buffers are reread; the final
partial shard follows the same rule. A restarted scan resumes
that checkpoint; duplicate IDs, count changes and incomplete scans fail. Both
writers must remain stopped until the reconciliation baseline is adopted.

`build_delta` validates both complete inventories, compares them using a bounded
SQLite cache, and persists an immutable candidate manifest. Newer source versions
and later updates of the same version become candidates; newer destination
versions stay intact. Source and destination counts alone never prove equality.
Completed plans retain stable checksums across repeat invocations. Only candidate
records require complete metadata and vector reads in the subsequent apply stage.

## Apply and final verification

`scholight store reconcile plan|apply|verify` requires explicit source/target
endpoints and collection IDs, model/dimension, stopped-writer declarations and
a dedicated S3 prefix. Inject `SCHOLIGHT_RECONCILE_SOURCE_TOKEN` and
`SCHOLIGHT_RECONCILE_TARGET_TOKEN` through a controlled process environment with
`SCHOLIGHT_DISABLE_DOTENV=1`; neither token is a command argument.

Apply reads full metadata and existing vectors only for planned candidates in
batches of 64. It saves both the destination before-image and the desired image
as float32 Parquet before writing. Existing destination resource flags remain
unchanged; new records start without unproven fulltext flags. Source or target
changes since planning cause a refusal. An interrupted batch accepts only its
saved before/after images, verifies every resulting field and vector, then commits
its checkpoint. Repeat invocations recheck committed batches.

Final verification compares a complete destination inventory against both the
source inventory and the original destination inventory, rejects missing or
lowered versions, checks the expected total and rechecks every copied vector.
Only `verification.json` with `complete: true` can support destination baseline
adoption. An `apply.json` checkpoint alone is not migration acceptance.

## Baseline and recovery-scope adoption

After `reconcile verify` succeeds, use `scheduler adopt-baseline --plan ...
--proof-sha256 ... --date ... --scope-start ... [--scope-end ...]` from the
private ingestion task while both consumer registrations are paused. The verify
command prints the canonical verification checksum. Adoption checks that proof
against the current collection IDs and model, and checks `--date` against the
still-paused legacy PostgreSQL cursor. Scope end defaults to the cursor date;
explicitly select the pause date to include later failed attempts without
expanding the reviewed start boundary.

The recovery set is the union of abstract delta candidates, deferred observations
and incomplete/failed legacy jobs within the reviewed interval. It retains audit
records and deduplicates by paper/version under the destination ID. It excludes
unrelated older failures and does not consult legacy success as proof of des
fulltext. All scope rows persist before adopting the cursor; idempotent queue
registration then completes before the release controller resumes consumers.

## Versioned publication and destination bindings

New manual publications use manifest version 2 with API, Web, Extract, metadata
and ingest ARM64 digests. Version 1 remains readable for application rollback;
it cannot start destination-aware ingestion. Both native CI architectures build
and start the ingest command without launching production work. The personal
foundation owns its additional immutable ECR repository.

`personal_binding.py` validates a separate, non-secret binding object at
`bindings/des/<operation>.json` in the personal release bucket. Upload it with
`If-None-Match: *`. It contains the exact endpoint, actual papers/chunks collection
IDs, retained Qwen model and dimension, three independent Secrets Manager ARNs and
version IDs, and the dedicated `recovery/des/<operation>` S3 prefix. Its canonical
collection identity matches the database's target key. A release plan pins the
binding byte hash; secret values never enter release artifacts. A legacy rollback
preserves the adopted connection while selecting lean mode and disabling both
metadata and fulltext consumers. An already adopted target cannot be changed by
an ordinary application release.

The adoption command writes `adoption.json` only after the full selected scope,
baseline and resumable queue are persisted. Supply its key when explicitly enabling
production ingestion. The release controller rejects a proof for another target
or one changed after planning. Initial destination binding always pauses inherited
source schedules, even if the old metadata registration was enabled.

Even an OAI `noRecordsMatch` response must parse as a complete, correctly namespaced
OAI document before it proves an empty date. A truncated error or maintenance HTML
cannot advance the daily cursor.

A completed fulltext receipt and the install journal's completion stage commit in
one PostgreSQL transaction. A conflicting vector checksum, chunk count, profile
configuration or recovery manifest rejects completion and leaves the job retryable;
existence of an unrelated receipt is never treated as successful verification.

Expired final attempts are retained as `dead` with `lease_expired`, because a killed
process cannot record its own failure. Inspect ECS termination and resource data
before explicitly retrying them. Cooperative cancellation releases the lease and
refunds that attempt. A same-version daily promotion updates the job's source as
well as priority, so it cannot consume the reserved aged-backfill claim slot.
