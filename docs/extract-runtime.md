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

For `auto` plus `main_markdown`, the complete static extraction used for the render
decision is also the final static result, including its metadata. A different output
format or a rendered DOM uses a new parse. The full extraction algorithm remains
the default; no fast-mode quality tradeoff is introduced. The internal setting
`SCHOLIGHT_EXTRACT_PARSE_REUSE=false` disables reuse for isolated ablation runs.

The download, parser and browser stages admit at most 8, 4 and 2 FIFO waiters,
with respective waiting limits of 2, 2 and 5 seconds. The total request deadline
and memory pause also constrain admission. Cancellation returns a granted permit
exactly once, including the grant/cancellation race. Queue depths, waits and
rejections are observable. `SCHOLIGHT_EXTRACT_QUEUEING=false` restores immediate
rejection for isolated ablation runs.

A static download retains its execution permit until its file has been parsed or
discarded. This bounds downloaded documents waiting for the serial parser and
prevents a fast downloader from overflowing the next stage. Download completion
seals the file and releases unused scratch reservation; rendered files do the
same before parsing. Configured download concurrency and browser concurrency do
not increase.

Static download and parsing can share one in-flight operation for an exact
URL/render/output key with no target headers or cookies. The pool holds at most
32 keys and eight callers per key, including its creator. Each caller has its own
deadline and cancellation; only the last departing caller cancels and awaits the
owned work. The shared operation has its own 50-second work deadline plus cleanup.
Rendering is always independent and is never merged, including after shared SPA
detection. `SCHOLIGHT_EXTRACT_SINGLEFLIGHT=false` disables merging for ablation.

Each caller logs its join status and an opaque static-work ID. A separate
`static_work` completion and `StaticWorkDownloadBytes` measure actual shared work,
including work whose original caller disconnected. Joiners record zero additional
download bytes. Do not count static-work completions as public requests.

The API owns one internal HTTPX client for its lifespan, with per-call token,
request-ID and budget headers. External static requests share only a TCP connector;
each fetch has its own session/cookie jar, preserving cookies within that request's
redirect chain without carrying them into another caller.
Requests with caller-supplied target headers or cookies own a private connector,
so connection-bound authentication cannot cross callers. New connections still use
the policy-enforcing resolver, and every URL and redirect is validated even when a
connection is reused. Environment proxies remain disabled. Memory reclamation and
shutdown close the external connector. `SCHOLIGHT_EXTRACT_CONNECTION_REUSE=false`
disables both pools for isolated ablation runs.

Only static GETs without target headers/cookies can retry, at most once, after a
transient connection failure or HTTP 429/500/502/503/504. TLS certificate failures,
permanent DNS errors and read timeouts are not automatically retried. Full Jitter
starts at 200 ms with a 1-second cap. A valid `Retry-After` supplies a minimum delay;
if it cannot fit the remaining budget, return the original failure without retrying.
Both attempts, their redirects and waiting share the fetch and request deadlines.
The public aiohttp middleware boundary disables implicit transport replay, so the
library cannot add attempts beyond this budget. Browser work is never retried.

The production supervisor streams downloads into exclusive files below
`SCHOLIGHT_DATA_ROOT/extract-spool`. It reserves at most 256 MiB of scratch
capacity, including in-flight input and result files. Capacity is checked before
allocation and each write has an individual limit. Success, failure and
cancellation release ownership; startup removes stale owned files while holding
an exclusive directory lock. No full document is placed in an IPC message.

One serial parser worker handles HTML, text and PDF. A separate serial browser
worker owns Chromium. Startup checks actual PDF imports and browser launch before
readiness. Parsing-library caches are cleared after each parse.
The parser first collects and freezes its startup-only Python object graph before
reading any job. Per-job library cache reset and garbage collection still run,
but do not repeatedly scan process-lifetime imports. New request objects and
reference cycles remain collectible; no job is added to the permanent generation.
This uses Python's [GC permanent generation](https://docs.python.org/3.11/library/gc.html#gc.freeze)
only inside the parser child, never in the API or browser worker.
The bounded worker lifetime also bounds the frozen startup state. Each worker is
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

Full Trafilatura extraction remains the deployed default. The private parser
worker accepts an explicit `fast_html` experiment flag; the public/internal HTTP
models and runtime assembly do not enable it. Compare this candidate against the
full mode using annotated content and native worker CPU measurements before any
future activation. Faster execution alone cannot waive the quality gate.

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

`MemoryOOMKills` reports the cumulative `memory.events:oom_kill` count for the
container, including child workers. A worker can be killed while the supervisor
and ECS task remain alive, so task exit status alone cannot establish zero OOM.
Use the maximum counter for each task/log stream, never sum repeated samples or
subtract away a nonzero first sample. Review any nonzero value before acceptance
and apply the rollout's rollback criteria. See the
[Linux cgroup-v2 memory events contract](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory-interface-files).

At 640 MiB working set, new requests and stage transitions stop, the shared cache
is cleared, and workers are reclaimed. Admission resumes below 512 MiB after
reclamation finishes. Liveness remains independent of this temporary backpressure.
All workers remain within the existing 768 MiB container hard limit; no ECS,
Identity, schema, ingestion binding or shared infrastructure change is required.

Each executing task owns one memory reservation. Admission requires a fresh
container working-set sample plus all retained reservations to remain at or below
640 MiB. A phase replaces its prior reservation atomically; a failed increase
retains the old ownership until cleanup. Queue waiters reserve no execution
memory or result-file allowance. Download estimates grow with actual decoded
bytes, parsing selects an input-size/MIME envelope (including PDF signature
sniffing), and browser work has a separate envelope. Browser completion transfers
the reservation to its output file before waiting for parsing. All success,
failure and cancellation paths release the reservation once. Singleflight
waiters share the execution owner's reservation.

A cold worker also owns a separate startup allowance from before process creation
until readiness; the container working set then accounts for its resident heap.
The downloaded-input allowance remains owned during startup. Job admission and
result-file allocation happen after readiness, when a fresh working-set sample
includes the worker's resident heap; startup and execution peaks are not charged
at the same time. Warm worker calls need no startup allowance. Initial readiness,
scheduled recycling and replacement of an
idle crashed generation all use the same path. Failed or cancelled startup keeps
the allowance until the owned process family has been reaped.

If retained heaps leave insufficient room for a new envelope below the physical
high watermark, the guard schedules reclamation after all execution reservations
and worker slots become idle. It pauses admission before cleanup and resumes
below 512 MiB. Soft-pressure reclamation runs at most once per 30 seconds; a job
that cannot fit even without competing reservations must not interrupt active
work. Warm-phase competition alone, or an envelope exceeding the whole budget,
does not request reclamation. A rejected cold start may request idle reclamation
because its downloaded input already owns an allowance. The physical 640 MiB emergency
guard remains immediate.
`MemoryIdleReclaim` counts these deferred recoveries.

The estimate is conservative: it adds a complete phase envelope to measured
memory even when some allocations are already reflected in the working set.
`MemoryReservedBytes` is sampled once per second; rejected growth increments
`MemoryReservationRejected`. The model in `reservations.py` is a release-gated
calibration candidate. Validate its fixed costs and input multipliers against
native same-resource peak measurements before accepting B; synthetic estimates
alone are not evidence that the 640 MiB peak gate passes.

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
