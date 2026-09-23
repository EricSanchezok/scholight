# Extract experiments

Run from the repository root with `SCHOLIGHT_DISABLE_DOTENV=1 uv run --no-sync
python scripts/extract_benchmark/run.py --image IMAGE --output data/extract-benchmark/RUN`.
The default is a four-hour, 2,400-request mixed soak with random seed 42.
For corpus preflight use `--seconds 0 --requests 48`.

Use native Linux ARM64 / Python 3.11 images built from each exact revision.
The runner records immutable image metadata, configuration, corpus checksums,
all response outcomes and content, per-second cgroup working set/anon/file/OOM
events, and final container state/logs. Run versions sequentially on the same
machine; alternate baseline/A/B for at least five cold/warm performance rounds.
Keep timing assertions out of ordinary unit tests.

Use `--mode cold` for unique request keys and `--mode warm` for a separate recorded
48-case warmup followed by fixed-key measurements. The default `mixed` mode keeps
the seeded 20% hot / 80% cold soak workload. `--concurrency 2`, `4`, `8` or `16`
submits bounded waves from a separate client cgroup; `--mode duplicate` submits one
exact static key per wave. All outcomes remain in the evidence, including rejected
and timed-out requests. Run at least five alternating cold/warm rounds per version
when the host is otherwise quiet. Do not use build-overlapped soak timings for the
latency gate.

`--mode short --concurrency 8 --requests 8 --seconds 0` isolates the eight-short-
request queue acceptance case. Fixture logs include each actual request path and
peer connection address, so duplicate-work and connection-reuse claims can be
checked against upstream observations as well as service telemetry. No request
headers, cookies or production addresses are recorded by this owned fixture.

The 48 self-owned fixtures cover articles, documentation, Chinese, short pages,
tables, code, JavaScript, JSON/XML and valid/malformed PDFs. Expected evidence
tokens are a smoke gate; they do not substitute for paragraph/table/link quality
comparison when considering a parser algorithm change. Production baseline
PDF failures remain recorded as failures, not excluded from aggregate results.

Each run uses a disposable **internal** Docker bridge with a public-address-shaped
subnet. It cannot route to the Internet. Literal fixture addresses exercise the
unmodified SSRF validator and real connector/browser. No policy override is
installed into the tested image. Fixture server and load generator run in
separate containers. No host port or production service is used. The measured
Extract container retains one replica, 768 MiB hard limit, 256 MiB reservation,
128 CPU shares, fetch concurrency 2, browser concurrency 1, and 32 MiB cache.
The small identical memory-probe process is included in every variant's cgroup.

Do not run multiple benchmark variants at once: the subnet intentionally conflicts
and creation must fail. Cleanup removes only resources created by that invocation.
Output directories must not exist before a run, preventing accidental replacement
of earlier evidence. Credentials in this harness are fixture-only constants.

For mixed-version wire checks, run `uv run python
scripts/extract_benchmark/mixed_versions.py --output data/extract-benchmark/mixed-A`.
The baseline defaults to the frozen production SHA above; fetch that revision if
using a shallow checkout. Both peers import their own full source tree in separate
Python processes and communicate through a real Unix socket. Checks cover unchanged
request JSON, optional headers, response/error mapping, immutable pagination and
cross-actor rejection in both directions. The document producer is a fixed stub;
this is a wire check, not an authentication, database or extraction-quality test.

For the optional fast parser candidate, mount this directory into the final B
image and run `/app/.venv/bin/python /benchmark/parser_compare.py --output
/results/parser --rounds 5` with an output volume. Keep Linux ARM64, Python 3.11
and the same container limits. The harness alternates complete/fast parsing in
supervised workers, records every output and CPU time, and resets parser caches
after each job. It adds 18 authored, annotated HTML pages to the frozen 48 cases.
Chinese characters and Unicode words form a multiset precision/recall score;
critical paragraph/code/table/link checks and full-mode line retention are
separate gates. The six JS parser fixtures use their exact authored hydration
payload; this does not replace real browser integration tests. Report HTML CPU
improvement and total mixed-corpus CPU separately. A `--allow-host --rounds 1`
development smoke is explicitly ineligible for acceptance. Quality must pass
and HTML parsing CPU must improve by at least 20%; runtime remains in full mode
until a separately reviewed activation.

The candidate follows [Trafilatura's documented fast mode](https://trafilatura.readthedocs.io/en/latest/extraction-overview.html),
which skips backup extraction. These fixtures establish reproducibility and
identify regressions; they cannot establish universal quality on arbitrary sites.

## Offline cache policies

`uv run python scripts/extract_benchmark/cache_replay.py --output
data/extract-benchmark/cache-synthetic` compares LRU, byte-weighted W-TinyLFU and
GreedyDual-Size over five seeded hotspot, scan and repeated-burst traces. All use
600-second write TTL, 32 MiB and at most 1,024 entries; hits never renew TTL.
Sizes represent retained document objects, with additional conservative policy
overheads (not measured container RSS). Reports include every request, hit ratio,
saved observed/synthetic execution cost, charged memory and final entry count.

The W-TinyLFU reference has a fixed 1% LRU admission window, an 80% protected main
SLRU segment, four-bit Count-Min counters, a Bloom doorkeeper and periodic aging.
For variable sizes it compares the candidate frequency with the sum of frequencies
of all required victims. It is a transparent offline variant, not a port of
Caffeine's adaptive implementation. GDS uses `H = L + cost / retained_size` and
advances inflation `L` on eviction; a bounded linear minimum scan avoids retaining
stale heap entries. These implementations are never imported by production.
References: [TinyLFU paper](https://arxiv.org/abs/1512.00727),
[Caffeine design](https://github.com/ben-manes/caffeine/wiki/Design),
[GreedyDual-Size algorithm](https://static.usenix.org/publications/library/proceedings/usits97/full_papers/cao/cao_html/node8.html).

Pass `--trace PATH` for anonymized `extract_completed` JSONL collected after A.
Only existing opaque cache identifiers are accepted. The importer excludes
canaries, credentialed calls, failures and hits without a previously observed miss
cost, and reports each exclusion. Completion order is only an approximation to
concurrent arrival order, and rotating instance keys prevent joining across
restarts. Historical logs without identifiers cannot establish real reuse;
synthetic keys must never be described as an actual production cache trajectory.

## Alternating matrices and analysis

`matrix.py --baseline BASELINE_IMAGE --reliability A_IMAGE --output DIRECTORY`
runs five alternating cold/warm rounds. Add `--efficiency B_IMAGE --ablations`
for B plus each independently disabled optimization and 2/4/8/16-way cold/duplicate
bursts. The exact sequential plan is saved before execution; only one variant runs
at a time. `run.py --disable parse-reuse|singleflight|queueing|connections` applies
one documented runtime feature switch. Metadata records the switch and fixture
HTTP version. Soaks default to the original fixture HTTP/1.0 behavior; latency
matrices explicitly use HTTP/1.1 so connection reuse is measurable. All compared
variants in a matrix use the same protocol.

`analyze.py DIRECTORY` writes `analysis.json` for a run or matrix. It retains all
statuses and quality failures, reports ordinary successful P95 against the explicit
set of baseline-supported cases, and includes every PDF error in overall counts.
Memory analysis excludes request intervals with a 250 ms margin, skips the first
ten minutes before the initial idle-hour median, and compares it with the final
hour. It reports sample counts and cannot pass incomplete four-hour/2,000-request
soaks. A single successful latency gate does not substitute for failure, queue,
quality, cleanup, memory or production observation gates.

The analyzer also compares every paired baseline-supported request's complete
content, metadata, warnings and wire field types. Only collection timestamps and
the intentionally corrected `source_bytes` value are excluded from value equality;
their types are still checked. Rejections and missing responses fail this paired
gate. Reports group success latency by MIME and actual rendered state, and retain
unclassified failures separately. Content differences require explicit quality
review; passing a few expected marker strings cannot hide missing paragraphs.

## Isolated faults and memory calibration

`run.py --mode faults --seconds 0 --requests 1` uses the same isolated fixture
network for slow responses, slow/excessive redirect chains, client disconnects
and recovery requests. Its `faults.json` records actual response deadlines; this
mode is a lifecycle check, not a latency benchmark. Use `run.py --mode worker-faults
--seconds 0 --requests 1` to run the final image with 768 MiB and CPU shares 128,
stop a real parser, kill an idle worker and Chromium, and race eight close callers.
The runner sets `SCHOLIGHT_BENCHMARK_CONTAINER=1`; the probe also requires the exact
768 MiB cgroup hard limit. A slow navigation must first be observed by the isolated
fixture before killing Chromium, proving loss during active work. The probe checks
owned process-group termination/reaping and recovery; never run it on a production
task or in the host namespace.

Run `/app/.venv/bin/python /benchmark/calibrate.py` inside a native B image with
the same limits, no network, a mounted `/results` directory and this directory
mounted at `/benchmark`. It warms the browser, restarts the parser before each
sample, and takes 10 ms cgroup measurements across three repetitions of the frozen
non-JS corpus plus scaled prose, dense DOM, tables, Chinese, text and PDF streams.
The measured parent imports the production runtime dependency graph. The probe
cancels its owned task at 640 MiB or 45 seconds and leaves partial evidence for
review. Recommendations use observed fixed/size costs with a 50% margin and an
additional 8 MiB fixed allowance. This finite parser corpus cannot bound arbitrary
compressed PDFs; actual mixed-load soak and download/browser phase measurements
remain required before the model is accepted. Calibration is not a production
stress test and does not change runtime coefficients automatically.

## Serial production canary

`canary.py setup --base https://HOST --login-file PRIVATE_LOGIN_JSON --state-file
PRIVATE_STATE_JSON --output REPORT_JSON` logs in through the normal account API,
checks the designated email, and issues the named temporary Access Key. Login input
contains only `email` and `password`. Both credential files must live outside
tracked source and have owner-only permissions. The state contains secrets and
must never be attached as evidence. Setup refuses duplicate active canary names.

Use `canary.py run` with the same base/state and a new output report after release,
then every six hours. Every HTTP operation is serial and spaced by at least ten
seconds, including auth and MCP initialization. Checks cover REST/MCP static,
PDF and JS; immutable pagination/replay; cache reuse; cursor tampering; login and
search. Cross-key isolation briefly issues a second key for the same user, verifies
that it cannot read the primary key's cursor, then revokes it immediately. A pending
cleanup ID remains in private state if interrupted. This complements cross-user
isolation tests in the isolated suite; it does not impersonate a second production
user. `canary.py revoke` removes pending auxiliary keys, revokes the primary key,
logs out this canary session and removes its private state. Retain the login file
only for authorized recovery and remove the task's private copy after acceptance.

Reports contain status, latency and **server-generated response request IDs**, not
credentials, headers or response bodies. Public middleware assigns these IDs;
client-provided prefixes are not authoritative. Pass every report with repeatable
`cache_replay.py --canary-report REPORT_JSON` arguments when importing actual logs.
Also exclude those exact IDs when computing natural-traffic production metrics.
Transport failures lacking a response ID remain explicit unidentified failures;
do not silently count them as natural evidence or erase them from acceptance.
