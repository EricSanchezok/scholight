# Lean runtime and recovery boundaries

Scholight defaults to `SCHOLIGHT_RUNTIME_PROFILE=lean`. Only `arxiv_papers` is
required. Standard is the only available mode in lean; `strength` remains optional
and defaults to Standard. Thorough requests are rejected before quota or search.
History preserves its original mode; an unavailable historical mode displays a
message and requires an explicit Standard submission.

Survey is unavailable in lean mode, including direct API calls, worker startup,
notifications, cleanup, and event control. Existing data and full implementation
remain. `full` enables full-text recovery and hermetic regression tests. Public
Thorough additionally requires its explicit opt-in described below. Retained
Survey reference resolution uses Standard through the public MCP contract.
Web Extract remains independent.

Metadata synchronization records each observed paper/version in
`scholight.deferred_fulltext` after the metadata write, before advancing the daily
cursor. Replaying a failed day is idempotent. The ledger is not a runnable queue;
existing ingestion jobs and resource flags remain untouched. A stored `has_chunks`
flag does not prove that a revision seen during lean operation has been processed.

`SCHOLIGHT_METADATA_SYNC_BATCH_SIZE` defaults to 64 (range 1–512). Metadata
embeddings, Zilliz writes and deferred records complete one batch at a time;
generated vectors are released before the next batch. Only a fully successful
day advances the cursor. Failed later batches replay earlier idempotent writes;
the source day's scalar metadata remains in memory, but its vectors do not.

After restoring the full-text corpus, run `scholight scheduler resume-fulltext
--limit 500` with the full profile to preview recovery, then explicitly add
`--apply`. Successful matching jobs retire ledger entries; pending work remains
recorded. Repeated bounded runs progress without duplicating successful jobs.

Migration 015 is additive and owned solely by Scholight. It leaves existing
migration checksums, `auth`, and existing queue contracts unchanged. Apply it
before using the new metadata image; rolling back the application does not
require a down migration.

Standalone Survey maintenance/rerun entrypoints also reject lean mode before
opening database or artifact connections. Usage charts and allowances preserve
Standard and Thorough history; existing quota limits remain unchanged.

The internal search CLI also requires the full profile for levels above 1.
Local archives fsync shards and directory entries before manifest checkpoints;
S3 manifests use conditional writes after durable object checksum verification.

## Public Thorough opt-in

Full mode does not automatically expose Thorough. Set
`SCHOLIGHT_PUBLIC_THOROUGH_ENABLED=true` only after validating both collections;
see [search modes](search-modes.md). Lean continues to advertise only Standard.
Survey remains independently disabled.
