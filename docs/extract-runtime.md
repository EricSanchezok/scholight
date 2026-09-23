# Web Extract runtime

Web Extract serves the same document contract through authenticated REST and MCP.
The internal Extract service has a separate process and never owns product data.

## Cache and immutable pagination

The public document cache in Extract defaults to 32 MiB
(`SCHOLIGHT_EXTRACT_CACHE_MAX_BYTES`). Requests with target headers or cookies
cannot use this shared cache. API pagination snapshots have an independent 64 MiB
limit (`SCHOLIGHT_EXTRACT_SNAPSHOT_MAX_BYTES`). Both caches retain at most 1,024
entries, expire after 600 seconds, and sweep expiry every 30 seconds while idle.
LRU eviction also runs on access and insertion.

Byte accounting conservatively includes retained Python strings (including wide
Unicode), nested metadata, container objects and index overhead. It measures
retained objects, not UTF-8 wire size. Oversized documents cannot evict other
snapshots. A response requiring a snapshot that does not fit returns the existing
`extract_cursor_unavailable` error. Cache budgets do not represent total process
memory; in-flight parsing and serialization require additional capacity.

Each opaque cursor contains a random snapshot identifier, offset and HMAC. Reading
the same cursor is deterministic and creates no server-side cursor records. Both
metadata and content remain immutable. The snapshot validates the complete actor
identity (including Access Key), expiry and eviction status. Cursor signatures use
a process-local random secret; API restarts invalidate cursors as before. No
database, response-model or cross-version internal JSON changes are required.

## Execution isolation

The production supervisor streams downloads into exclusive files below
`SCHOLIGHT_DATA_ROOT/extract-spool`. It reserves at most 256 MiB of scratch
capacity, including in-flight input and result files. Capacity is checked before
allocation and each write has an individual limit. Success, failure and
cancellation release ownership; startup removes stale owned files while holding
an exclusive directory lock. No full document is placed in an IPC message.

One serial parser worker handles HTML, text and PDF. A separate serial browser
worker owns Chromium. Startup checks actual PDF imports and browser launch before
readiness. Parsing-library caches are cleared after each parse. Each worker is
terminated after 100 tasks and its successor starts only after it has exited.
On cancellation the supervisor kills the worker and its descendant process
groups, including Chromium's detached group. Linux subreaping prevents orphaned
browser children from accumulating after termination. All processes remain
inside the same Extract container memory limit.

Browser startup, context creation, policy callbacks and close races use stable
errors. Cleanup errors cannot replace the original document error. The Python
library's injectable in-process engine remains available for isolated unit tests;
the deployed runtime always injects supervised workers.
