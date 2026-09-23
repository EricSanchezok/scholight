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
