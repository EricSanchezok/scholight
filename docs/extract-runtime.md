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
Cancellation during process creation retains the returned process handle before
cleanup, including repeated cancellation. Concurrent close callers share one owned
cleanup task; cancelling a caller cannot abandon that task. Shutdown waits for
adopted descendants to exit and reaps them within
the same two-second cleanup budget. Native image tests assert that Chromium's
detached groups leave neither running processes nor zombies behind.

Browser startup, context creation, policy callbacks and close races use stable
errors. Cleanup errors cannot replace the original document error. The browser
worker is retired after unexpected lifecycle or cleanup failures. Route
callback failures are confined to their context and abort the affected resource.
The Python library's injectable in-process engine remains available for isolated unit tests;
the deployed runtime always injects supervised workers.

## Deadlines, cancellation and telemetry

Public REST/MCP extraction uses one absolute 55-second operation deadline (or the
configured shorter request timeout). API forwards optional
`X-Scholight-Request-Id` and `X-Scholight-Budget-Ms` headers. Internal extraction
caps that budget at 52 seconds and reserves its last two seconds for process/file
cleanup. Queueing, redirects, download, browser work and parsing consume the same
remaining budget. A disconnected internal client cancels the active operation.
REST and MCP HTTP client disconnects propagate cancellation through the internal
HTTP call; disconnect handling completes as a controlled 499 lifecycle event.
MCP's JSON response transport needs an explicit disconnect monitor while awaiting
the tool result. One ASGI reader and a one-message queue preserve body chunks and
keep disconnect signals scoped to their request. MCP protocol tool cancellation
also retains the SDK's task cancellation path.
If the ASGI caller itself is cancelled, the owned JSON request gets up to two
seconds to deliver its cancelled result internally and terminate its stateless
SDK session. Closed-socket sends do not interrupt that cleanup. This prevents
cancelled Extract calls from accumulating idle MCP server tasks.
Both older APIs without these headers and older Extract services ignoring them
retain the same JSON contract. Old services retain their older resource behavior.

One `extract_completed` event per public or internal operation covers success,
controlled error, timeout, cancellation and unexpected failure. Public records
distinguish initial extraction from pagination. Correlation identifiers are
sanitized and appear only in logs. No target URL, query, cookie, header, document
content or raw exception message is recorded. Records include MIME category,
requested render mode, cache eligibility and hit status, upstream status,
stage elapsed time and parser CPU time. EMF uses bounded service/outcome dimensions.
Telemetry sinks are best effort and cannot replace a response or its original error.

Cache-eligible internal completion logs include an opaque HMAC key identifier,
the conservative retained entry size and TTL, and elapsed operation time. The
HMAC secret is random for each service instance and never persisted or logged;
identifiers cannot be joined across restarts or reversed by hashing guessed URLs.
Credentialed requests omit the identifier and size. These fields allow an offline
cache trace to preserve repeat patterns without logging URLs. They are log fields,
never metric dimensions. A hit does not reveal the counterfactual extraction cost;
offline replays must carry forward only an observed miss cost and label incomplete
or censored traces explicitly.

Static transfer bytes, decoded source-document bytes and rendered DOM bytes are
separate measurements. A cache hit records zero transfer bytes. Cgroup-v2 memory
sampling reports working set (`memory.current - inactive_file`), anonymous memory,
file memory, parser/browser family RSS, activity and worker start gauges each
second. Family RSS can double-count shared pages and is diagnostic only; the
container working set controls admission. Missing cgroup measurements fail closed.
Local macOS development uses a conservative process RSS bound instead.

At 640 MiB working set, new requests and stage transitions stop, the shared cache
is cleared, and workers are reclaimed. Admission resumes below 512 MiB after
reclamation finishes. Liveness remains independent of this temporary backpressure.
All workers remain within the existing 768 MiB container hard limit; no ECS,
Identity, schema, ingestion binding or shared infrastructure change is required.

## Production acceptance fixtures

The web image serves three tiny, self-owned fixtures below `/extract-canary/`:
`static.html`, `document.pdf` and `javascript.html`. They have no external requests
or account state. Every response, including errors, carries `X-Robots-Tag:
noindex, nofollow, noarchive` and `Cache-Control: no-store`; robots.txt also excludes
the path. Regenerate them with `uv run python
scripts/extract_benchmark/canary_fixtures.py` after installing the locked frontend
dependencies with `npm ci --prefix frontend`. Native web image smoke verifies these
headers. Do not add these operational samples to product navigation or sitemaps.

Acceptance calls use the public authenticated REST/MCP endpoints with a dedicated
temporary Access Key and distinct request IDs. Run serially at no more than one
call per ten seconds, repeating the fixed suite every six hours during observation.
Keep credentials out of command-line arguments, evidence and reports. Separate
canary request IDs from natural traffic and revoke the key after acceptance.
Load tests and fault injection remain confined to the isolated benchmark network.
