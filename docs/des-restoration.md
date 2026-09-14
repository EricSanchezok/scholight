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
