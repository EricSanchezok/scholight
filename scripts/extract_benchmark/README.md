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
