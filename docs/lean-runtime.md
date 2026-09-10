# Lean runtime and recovery boundaries

Scholight defaults to `SCHOLIGHT_RUNTIME_PROFILE=lean`. Only `arxiv_papers` is
required. Standard is the sole public search behavior; `strength=standard` remains
an optional deprecated compatibility field. Thorough input fails validation before
quota or search. New clients omit strength. Existing history and response fields
remain readable; replaying historical full-text queries uses the current search.

Survey is unavailable in lean mode, including direct API calls, worker startup,
notifications, cleanup, and event control. Existing data and full implementation
remain. `full` enables explicit recovery and hermetic regression tests, but does
not restore public Thorough search. Web Extract remains independent.

Metadata synchronization records each observed paper/version in
`scholight.deferred_fulltext` after the metadata write, before advancing the daily
cursor. Replaying a failed day is idempotent. The ledger is not a runnable queue;
existing ingestion jobs and resource flags remain untouched. A stored `has_chunks`
flag does not prove that a revision seen during lean operation has been processed.

After restoring the full-text corpus, run `scholight scheduler resume-fulltext
--limit 500` with the full profile to preview recovery, then explicitly add
`--apply`. Successful matching jobs retire ledger entries; pending work remains
recorded. Repeated bounded runs progress without duplicating successful jobs.

Migration 015 is additive and owned solely by Scholight. It leaves existing
migration checksums, `auth`, and existing queue contracts unchanged. Apply it
before using the new metadata image; rolling back the application does not
require a down migration.
