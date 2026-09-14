# Public search modes

Standard remains the default for omitted `strength`. HTTP and MCP accept
`standard` and `thorough`; the paper response shape and both quota overrides are
unchanged. Reservations, refunds, history, usage, and metrics use the requested
mode. A Thorough dependency failure returns `thorough_search_unavailable` and
never executes a second Standard request.
The current allowance summary adds Standard and Thorough consumption, excluding
Survey and failed searches whose quota was refunded.

## Configuration and discovery

`SCHOLIGHT_PUBLIC_THOROUGH_ENABLED` defaults to false. Enabling it requires
`SCHOLIGHT_RUNTIME_PROFILE=full`; lean plus this flag is a startup configuration
error. Full mode alone does not expose Thorough. Survey uses its own independent
off settings and is not enabled by either search option.

`/capabilities` adds `search_modes`, either `["standard"]` or
`["standard", "thorough"]`. The frontend assumes Standard when the new field is
absent or discovery fails. Explicit Thorough URLs and history remain Thorough;
if unavailable they show a message without submitting a substitute search.
URLs, result cache keys, loading states, and filter changes preserve the mode.
History reruns preserve the original question and search filters as well. The
history filter only updates `/history` parameters; during its exit animation it
must not overwrite the destination search URL with the history-list filter.

## Read-only query behavior

The Python SDK Loaded enum and the REST Loaded value are recognized. Searches
never create, load, or repair collections. An unloaded or unavailable chunks
collection causes Thorough to fail and refund its reservation. Collection
maintenance is an explicit operator action with separate credentials. Readiness
checks the required primary keys, vector types/dimensions, completed search indexes
and load states. Process health remains independent of these external probes.

## Verification

Tests cover actual mode accounting for anonymous, web, Access Key and delegated
actors, unchanged Standard defaults, failure compensation without fallback,
read-only load handling, and mode-preserving UI submission and history URLs.
Production activation additionally requires target data reconciliation and the
reviewed personal release process; merging this change alone does not enable it.

If Thorough becomes unavailable, an existing Thorough query stays selected.
Submitting it shows a short availability message; users explicitly select
Standard before issuing an abstract search. Filters cannot silently change modes.
