# Native image smoke

`smoke.sh` expects locally built `scholight-{api,web,extract,metadata}:lean-ci`
images. It starts unprivileged processes without host ports and cleans up only
its own named containers. API lifecycle/search and metadata external boundaries
are replaced with deterministic local test doubles. Chromium starts inside the
Extract image. Both native Linux architectures run this in CI.

This verifies runtime compatibility, not cloud deployment or real model quality.
Real collection round trips use the separate `tests/archive_integration` topology.
